"""The guard that stops one live view evicting another's dispatch slot.

py-cord keys component dispatch by (component_type, message_id, custom_id) in one
process-wide dict, and ephemeral views register with message_id=None — so a fixed
custom_id makes every live instance share a slot, and the newest silently evicts the
rest. That has been fixed by hand three times here (quiz views, settle selects,
staked bet-cap panel), each time by removing ids from one batch of call sites and
missing the next batch.

These tests pin the guard rather than any one batch: what it catches, what it
deliberately does not, and — most importantly — that it is actually attached to
py-cord's real registration path, since a diagnostic that silently stops running is
worse than none at all.
"""
from unittest.mock import patch

import discord
import pytest
import pytest_asyncio
from discord.ui.view import View, ViewStore

from conftest import make_view_store
from helpers.view_dispatch_guard import (
    DispatchCollision,
    install_dispatch_collision_guard,
)


@pytest_asyncio.fixture(autouse=True)
async def _guard_installed():
    """Install the guard for these tests, then put py-cord back as it was.

    The guard patches the ViewStore CLASS, so without a teardown it stays installed
    for the rest of the pytest session and arms the TEST_MODE raise for every file
    that runs afterwards. That is a trap for exactly the test this bug class invites
    next: a regression test for the following hand-fixed instance would want to
    register two colliding views and assert on the eviction, and would instead fail
    with an order-dependent DispatchCollision pointing at unrelated code.

    Async so the teardown runs while the loop is still open — View.stop() cancels a
    timeout task, which needs one.
    """
    original = ViewStore.add_view
    install_dispatch_collision_guard()
    yield
    ViewStore.add_view = original


def _view(track, custom_id, *, timeout=300, cls=None):
    """A view holding one button, with or without a caller-supplied custom_id."""
    view = (cls or View)(timeout=timeout)
    kwargs = {"label": "x"}
    if custom_id is not None:
        kwargs["custom_id"] = custom_id
    view.add_item(discord.ui.Button(**kwargs))
    return track(view)


def _as_test_mode(enabled=True):
    """Pin the raise-vs-log decision instead of inheriting it from the developer's
    .env. Without this the suite passes or fails depending on whether TEST_MODE
    happens to be exported — which is how the first version of these tests silently
    asserted nothing."""
    return patch("helpers.view_dispatch_guard.is_test_mode", return_value=enabled)


@pytest.mark.asyncio
async def test_two_live_ephemeral_views_sharing_an_id_are_caught(live_views):
    """The bug itself: same id, both alive, second would evict the first."""
    store = make_view_store()
    store.add_view(_view(live_views, "counterparty_select"), message_id=None)

    with _as_test_mode(), pytest.raises(DispatchCollision) as excinfo:
        store.add_view(_view(live_views, "counterparty_select"), message_id=None)

    assert "counterparty_select" in str(excinfo.value)


@pytest.mark.asyncio
async def test_auto_generated_ids_never_collide(live_views):
    """What the fix looks like: omit custom_id and py-cord assigns a unique one, so
    any number of instances coexist."""
    store = make_view_store()
    for _ in range(3):
        store.add_view(_view(live_views, None), message_id=None)

    assert len(store._views) == 3


@pytest.mark.asyncio
async def test_a_finished_view_does_not_hold_its_slot(live_views):
    """py-cord's remove_view pops a 2-tuple against a 3-tuple keyspace, so stopped
    views are never actually removed from the dict. Reporting those as collisions
    would make the guard noise the moment anyone's menu timed out."""
    store = make_view_store()
    stale = _view(live_views, "settle_debts")
    store.add_view(stale, message_id=None)
    stale.stop()

    with _as_test_mode():   # armed to raise, so silence here is meaningful
        store.add_view(_view(live_views, "settle_debts"), message_id=None)  # must not raise


@pytest.mark.asyncio
async def test_re_registering_a_persistent_view_is_not_a_collision(live_views):
    """bot.add_view(PublicSettleDebtsView()) runs inside on_ready, which fires again
    on every gateway reconnect: a fresh instance, the same fixed ids, timeout=None so
    the previous never reports itself finished. That is persistence working, not the
    bug, and flagging it would fire on every reconnect."""
    store = make_view_store()

    class Persistent(View):
        pass

    with _as_test_mode():   # armed to raise, so silence here is meaningful
        store.add_view(_view(live_views, "public_settle_debts_button", timeout=None, cls=Persistent),
                       message_id=None)
        store.add_view(_view(live_views, "public_settle_debts_button", timeout=None, cls=Persistent),
                       message_id=None)  # must not raise


@pytest.mark.asyncio
async def test_two_different_persistent_views_sharing_an_id_are_still_caught(live_views):
    """Persistence excuses re-registering the same view, not two different views
    laying claim to one id — that is the same lost update by another route."""
    store = make_view_store()

    class A(View):
        pass

    class B(View):
        pass

    store.add_view(_view(live_views, "shared", timeout=None, cls=A), message_id=None)
    with _as_test_mode(), pytest.raises(DispatchCollision):
        store.add_view(_view(live_views, "shared", timeout=None, cls=B), message_id=None)


@pytest.mark.asyncio
async def test_views_on_real_messages_are_keyed_apart_by_message_id(live_views):
    """Non-ephemeral sends carry a real message id, so the same custom_id on two
    different messages is not a collision — this is how persistent buttons work."""
    store = make_view_store()
    with _as_test_mode():   # armed to raise, so silence here is meaningful
        store.add_view(_view(live_views, "settle_debts:1"), message_id=111)
        store.add_view(_view(live_views, "settle_debts:1"), message_id=222)  # must not raise


@pytest.mark.asyncio
async def test_the_guard_is_attached_to_pycords_real_registration_path(live_views):
    """The failure mode this exists to prevent: a py-cord upgrade moves or renames
    add_view, the wrapper quietly stops being reached, and the bug returns with the
    tests still green. Asserting the attribute on the live class means an upgrade
    breaks this test rather than the guard."""
    assert getattr(ViewStore.add_view, "_draftbot_guarded", False), (
        "the guard is no longer wrapping ViewStore.add_view")


@pytest.mark.asyncio
async def test_installing_twice_does_not_stack_wrappers(live_views):
    """on_ready runs again on reconnect; wrapping a wrapper would report twice and
    make the log read like two separate bugs.

    Asserts the function object is unchanged rather than trusting a return value —
    "the wrapper was not wrapped again" is the property that actually matters.
    """
    already_installed = ViewStore.add_view

    install_dispatch_collision_guard()

    assert ViewStore.add_view is already_installed


@pytest.mark.asyncio
async def test_production_logs_the_collision_but_does_not_raise(live_views):
    """A diagnostic must never be the thing that breaks a live draft. In production
    the collision is a logged error, not an exception thrown into a component
    callback."""
    store = make_view_store()
    store.add_view(_view(live_views, "dup"), message_id=None)

    with _as_test_mode(False), patch("helpers.view_dispatch_guard.logger") as log:
        store.add_view(_view(live_views, "dup"), message_id=None)  # must not raise

    # ...but it must still say so: silence in production would mean the collision
    # goes unnoticed exactly where it costs a player their interaction.
    log.error.assert_called_once()
    assert "dup" in log.error.call_args.args[0]
