"""Ephemeral settle-flow views must dispatch per-instance, not per-class.

Same failure mode as the quiz views (see test_quiz_view_dispatch.py), in a
different module. py-cord's ViewStore keys live views by
(component_type, message_id, custom_id), and an ephemeral view sent via
interaction.response.send_message is registered with message_id=None — so
any custom_id shared between two live instances collapses them into ONE
process-wide dispatch slot. add_view() overwrites that slot, so the newest
registration silently evicts the older one, and the evicted message's
components dispatch to nothing: Discord shows "This interaction failed"
and the buttons are dead forever.

REPRODUCED — prod incident, 2026-08-12 23:37:28 (Lotus Lounge): a
counterparty selection was dispatched to a DIFFERENT user's view instance,
which logged the nonsensical "User 204717025363230720 selected counterparty
204717025363230720, balance: 0" (their own id was never an option in their
own menu) and then crashed building the next view: "In
data.components.0.components.0.options: Must be between 1 and 25 in length"
— the misrouted balance of 0 with no card positions yields an entity select
with zero options. Both halves of that chain have a test below.

NOT EXPLAINED BY THIS MECHANISM — prod incident, 2026-08-13 16:11:00: a
settlement confirmation was sent successfully and the user's Confirm click
produced no log line at all (neither the confirm handler's first statement
nor Cancel's); they retried 14s later, and the same pair had failed
identically the previous evening. test_concurrent_settlement_confirms_...
below PASSES against current code, which rules the collision out for that
dialog: py-cord generates a per-instance custom_id for decorator-declared
buttons. Note ViewStore.dispatch() returns silently when no view matches, so
a dropped interaction and one that never arrived are indistinguishable in
logs — separating them needs an on_interaction listener logging arrivals.

Only PERSISTENT views (timeout=None, re-registered on restart) may pin a
custom_id; ephemeral views must let py-cord generate one per instance.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio

import discord
from discord.ui.item import Item
from discord.ui.view import View, ViewStore

import debt_views.settle_views as sv
from debt_views.settle_views import (
    CounterpartySelectView,
    DebtorSelectView,
    SettleEntitySelectView,
    SettlementConfirmView,
    TransferCreditorSelectView,
)

SELECT = discord.ComponentType.string_select.value
BUTTON = discord.ComponentType.button.value

GUILD = SimpleNamespace(id=1, get_member=lambda _id: None)

# Views started under a running loop spawn a timeout task; stop them after
# each test so pytest doesn't report pending tasks at teardown.
_LIVE_VIEWS = []


@pytest_asyncio.fixture(autouse=True)
async def _stop_views():
    """Async so teardown runs while the loop is still open (View.stop()
    cancels the timeout task)."""
    yield
    for view in _LIVE_VIEWS:
        view.stop()
    _LIVE_VIEWS.clear()


def _track(view):
    _LIVE_VIEWS.append(view)
    return view


def _counterparty_view(user_id, counterparty_id):
    with patch.object(sv, "get_member_name_plain", lambda g, i: f"player-{i}"):
        return _track(CounterpartySelectView(
            user_id=user_id,
            guild_id="g1",
            balances={counterparty_id: -20},
            guild=GUILD,
        ))


def _confirm_view(user_id, counterparty_id):
    return _track(SettlementConfirmView(
        user_id=user_id,
        guild_id="g1",
        payer_id=counterparty_id,
        payee_id=user_id,
        amount=20,
    ))


def _entity_view(user_id, counterparty_id, net_balance=-20, positions=None):
    return _track(SettleEntitySelectView(
        user_id=user_id,
        guild_id="g1",
        counterparty_id=counterparty_id,
        net_balance=net_balance,
        counterparty_name_plain="them",
        counterparty_name_decorated="them",
        positions=positions if positions is not None else [],
    ))


def _transfer_creditor_view(user_id, counterparty_id):
    with patch.object(sv, "get_member_name_plain", lambda g, i: f"player-{i}"):
        return _track(TransferCreditorSelectView(
            user_id=user_id,
            guild_id="g1",
            creditors_with_transfers=[(counterparty_id, 20, [("d1", 20, 20)])],
            guild=GUILD,
        ))


def _debtor_view(user_id, counterparty_id):
    with patch.object(sv, "get_member_name_plain", lambda g, i: f"player-{i}"):
        return _track(DebtorSelectView(
            user_id=user_id,
            guild_id="g1",
            creditor_id=counterparty_id,
            creditor_name_plain="them",
            creditor_name_decorated="them",
            transferable_debtors=[("d1", 20, 20)],
            guild=GUILD,
        ))


def _ephemeral_settle_views(user_id, counterparty_id):
    """One instance of every per-user ephemeral view in the settle flow —
    extend when adding a new one so the per-instance-id invariant covers it.

    All three views this branch touched are here: leaving two of them out would have
    let a partial revert pass, since only the counterparty select was exercised.
    """
    return [
        _counterparty_view(user_id, counterparty_id),
        _entity_view(user_id, counterparty_id),
        _confirm_view(user_id, counterparty_id),
        _transfer_creditor_view(user_id, counterparty_id),
        _debtor_view(user_id, counterparty_id),
    ]


@pytest.mark.asyncio
async def test_ephemeral_settle_views_have_per_instance_component_ids():
    """No dispatchable component may share a custom_id across two fresh
    instances — a shared id is a shared, evictable dispatch slot."""
    for a, b in zip(_ephemeral_settle_views("u1", "c1"),
                    _ephemeral_settle_views("u2", "c2")):
        ids_a = {c.custom_id for c in a.children if c.is_dispatchable()}
        ids_b = {c.custom_id for c in b.children if c.is_dispatchable()}
        shared = ids_a & ids_b
        assert not shared, (
            f"{type(a).__name__} shares dispatch ids across instances: {shared}")


def _dispatched_to(store, component_type, custom_id, message_id, routed):
    routed.clear()
    store.dispatch(component_type, custom_id, SimpleNamespace(
        message=SimpleNamespace(id=message_id), data={"values": []}))
    return routed[0] if routed else None


@pytest.mark.asyncio
async def test_concurrent_counterparty_selects_dispatch_independently():
    """Two users picking a counterparty at the same time: each selection
    must reach the view holding that user's balances. Prod incident 1 —
    the misroute is what let one user's menu answer another user's view."""
    routed = []
    with patch.object(View, "_dispatch_item",
                      lambda self, item, i: routed.append(self)), \
         patch.object(Item, "refresh_state", lambda self, i: None):
        store = ViewStore(state=SimpleNamespace())
        view_a = _counterparty_view("u1", "c1")
        view_b = _counterparty_view("u2", "c2")
        store.add_view(view_a, message_id=None)   # as ephemeral sends do
        store.add_view(view_b, message_id=None)

        id_a = next(c.custom_id for c in view_a.children if c.is_dispatchable())
        id_b = next(c.custom_id for c in view_b.children if c.is_dispatchable())

        assert _dispatched_to(store, SELECT, id_a, 111, routed) is view_a
        assert _dispatched_to(store, SELECT, id_b, 222, routed) is view_b


@pytest.mark.asyncio
async def test_concurrent_settlement_confirms_dispatch_independently():
    """Two settlement confirmations live at once (two users, or one user
    settling with two counterparties in a row): each Confirm must reach its
    own view.

    This PASSES against current code — decorator-declared buttons already get
    a per-instance custom_id. Kept as a guard so nobody "fixes" the confirm
    dialog by pinning an id, and as the record that eviction is NOT the cause
    of the 2026-08-13 dead-Confirm report.
    """
    routed = []
    with patch.object(View, "_dispatch_item",
                      lambda self, item, i: routed.append(self)), \
         patch.object(Item, "refresh_state", lambda self, i: None):
        store = ViewStore(state=SimpleNamespace())
        view_a = _confirm_view("u1", "c1")
        view_b = _confirm_view("u2", "c2")
        store.add_view(view_a, message_id=None)
        store.add_view(view_b, message_id=None)

        def confirm_id(v):
            return next(c.custom_id for c in v.children
                        if getattr(c, "label", "") == "Confirm Settlement")

        assert _dispatched_to(store, BUTTON, confirm_id(view_a), 111, routed) is view_a
        assert _dispatched_to(store, BUTTON, confirm_id(view_b), 222, routed) is view_b



@pytest.mark.asyncio
async def test_an_ephemeral_send_really_registers_without_a_message_id():
    """The library contract that makes custom_id load-bearing here.

    Everything else in this file assumes ephemeral views are registered with
    message_id=None. If that were wrong — if they were keyed by message id like
    ordinary sends — the custom_id would not matter and none of this could happen.
    So it is pinned against py-cord itself (2.6.1) rather than restated as an
    assumption: store_view defaults message_id to None, ViewStore keys on the
    3-tuple, and with message_id None the custom_id is the only thing left
    separating two live instances.

    The last assertion is the fix: two instances must occupy TWO slots. Before the
    fix they shared one, and the newer silently evicted the older.
    """
    import inspect

    from discord.state import ConnectionState

    sig = inspect.signature(ConnectionState.store_view)
    assert sig.parameters["message_id"].default is None, (
        "store_view no longer defaults message_id to None — re-check the analysis")

    store = ViewStore(state=SimpleNamespace())
    view_a = _counterparty_view("u1", "c1")
    view_b = _counterparty_view("u2", "c2")
    store.add_view(view_a, message_id=None)
    store.add_view(view_b, message_id=None)

    assert all(k[1] is None for k in store._views), (
        "ephemeral slots key on message_id None, which is what leaves custom_id "
        "as the only discriminator")
    assert len(store._views) == 2, (
        "each ephemeral view needs its own dispatch slot; sharing one means the "
        "newer silently evicts the older")
