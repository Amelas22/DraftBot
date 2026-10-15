"""The serve's /inventory, and the audits that compare it to the ledgers.

What these protect, in the order it would hurt:

  * A reply that is not a usable inventory never reads as "the account holds
    nothing" -- that would report every card in every library missing.
  * A difference seen while a trade is moving is never called a mismatch.
  * The library's expected stock is owned minus out-on-loan, across every
    library scope sharing the account.
  * Stock checks treat an unlisted card as ZERO once the listing is complete,
    and keep the old present-if-unlisted reading only on the truncated /vault.
"""
from types import SimpleNamespace

import pytest
from aiohttp import web

from conftest import FakeLendingServe, a_library
from services import card_lending_service, debt_service, inventory_reconcile, wallet_service
from services.inventory_reconcile import (
    CLEAN, MISMATCH, UNREADABLE, UNSETTLED, CardLine, DriftAlerter, InFlight, compare,
    verdict_for,
)
from services.mtgo_tradebot_client import Inventory
from test_lending_client_contract import _json, a_serve

pytestmark = pytest.mark.asyncio

_GOOD = {"available": True, "custodian": "Team01", "at": "2026-10-15T12:00:00Z", "ageSec": 4,
         "consistent": True, "tix": 12, "filtered": False, "notHeld": [],
         "cards": [{"catId": 100, "name": "Lightning Bolt", "qty": 3},
                   {"catId": 200, "name": "lightning bolt", "qty": 1},
                   {"catId": 300, "name": "Counterspell", "qty": 2}]}


# ---------------------------------------------------------------------------
# Inventory.parse
# ---------------------------------------------------------------------------
async def test_a_good_reply_parses_and_folds_printings_by_name():
    inv = Inventory.parse(_GOOD)

    assert inv is not None
    assert inv.tix == 12 and inv.consistent
    assert inv.by_name() == {"lightning bolt": 4, "counterspell": 2}
    assert inv.by_name(key=str) == {"Lightning Bolt": 3, "lightning bolt": 1, "Counterspell": 2}


@pytest.mark.parametrize("body", [
    None, [], {"raw": "oops"},
    {**_GOOD, "available": False},
    {k: v for k, v in _GOOD.items() if k != "cards"},
    {k: v for k, v in _GOOD.items() if k != "tix"},
    {**_GOOD, "cards": [{"catId": 1, "name": "X"}]},
    {**_GOOD, "cards": ["Lightning Bolt"]},
    {**_GOOD, "cards": [{"catId": "1", "name": "X", "qty": 1}]},
])
async def test_anything_but_a_usable_inventory_is_none(body):
    assert Inventory.parse(body) is None


async def test_consistent_must_be_stated_true():
    assert Inventory.parse({k: v for k, v in _GOOD.items() if k != "consistent"}).consistent is False


# ---------------------------------------------------------------------------
# the client, over a real socket
# ---------------------------------------------------------------------------
async def test_refresh_is_sent_only_when_asked():
    seen = []

    async def handler(request):
        seen.append(request.query.get("refresh"))
        return web.json_response(_GOOD)

    async with a_serve({("GET", "/inventory"): handler}) as (client, _):
        assert await client.inventory(refresh=True) is not None
        assert await client.inventory() is not None

    assert seen == ["1", None]


async def test_a_serve_without_the_route_is_none_not_empty():
    async with a_serve({}) as (client, _):
        assert await client.inventory() is None


async def test_the_vault_fallback_is_never_consistent():
    vault = {"available": True, "tix": 9, "top": [{"name": "Swamp", "qty": 4}]}
    async with a_serve({("GET", "/vault"): _json(vault)}) as (client, _):
        assert await client.inventory() is None, "audits never fall back"
        inv = await client.inventory(fallback_to_vault=True)

    assert inv is not None and not inv.consistent
    assert inv.tix == 9 and inv.by_name(key=str) == {"Swamp": 4}


# ---------------------------------------------------------------------------
# compare / verdict / alerting -- pure
# ---------------------------------------------------------------------------
async def test_compare_folds_case_and_sorts_worst_first():
    missing, extra = compare(
        {"Lightning Bolt": 4, "Counterspell": 2, "Brainstorm": 1, "Ponder": 4},
        {"lightning bolt": 4, "Counterspell": 1, "Ponder": 1, "Island": 7})

    assert missing == [CardLine("Ponder", 4, 1), CardLine("Brainstorm", 1, 0),
                       CardLine("Counterspell", 2, 1)]
    assert extra == [CardLine("Island", 0, 7)]


@pytest.mark.parametrize("differs, consistent, in_flight, verdict", [
    (False, True, [InFlight("job", "j", "deposit", "")], CLEAN),
    (False, False, [], UNSETTLED),   # an old snapshot can agree with a stale ledger
    (True, True, [], MISMATCH),
    (True, False, [], UNSETTLED),
    (True, True, [InFlight("loan", "1", "out_pending", "")], UNSETTLED),
])
async def test_only_a_consistent_reading_is_judged(differs, consistent, in_flight, verdict):
    assert verdict_for(differs, consistent, in_flight) == verdict


async def test_drift_alerts_on_the_second_identical_mismatch_once_then_on_resolution():
    a = DriftAlerter()

    assert a.observe(MISMATCH, ("x", 1)) is None          # first sighting
    assert a.observe(MISMATCH, ("x", 1)) == "alert"
    a.confirm("alert", ("x", 1))
    assert a.observe(MISMATCH, ("x", 1)) is None          # already told
    assert a.observe(UNSETTLED, ("x", 9)) is None         # no evidence either way
    assert a.observe(CLEAN, ()) == "resolved"
    a.confirm("resolved", ())
    assert a.observe(CLEAN, ()) is None


async def test_a_changing_mismatch_waits_for_two_identical_readings():
    a = DriftAlerter()

    assert a.observe(MISMATCH, ("x", 1)) is None
    assert a.observe(MISMATCH, ("x", 2)) is None
    assert a.observe(MISMATCH, ("x", 2)) == "alert"


async def test_an_undelivered_alert_is_retried():
    a = DriftAlerter()
    a.observe(MISMATCH, ("x", 1))

    assert a.observe(MISMATCH, ("x", 1)) == "alert"       # not confirmed...
    assert a.observe(MISMATCH, ("x", 1)) == "alert"       # ...so said again


# ---------------------------------------------------------------------------
# the card audit, against the real ledger
# ---------------------------------------------------------------------------
async def _lend(library_id, borrower, name, qty):
    """Book cards out to a borrower the way a settled borrow does."""
    await debt_service.create_card_loan(
        guild_id=wallet_service.library_scope(library_id),
        lender_id=wallet_service.HOUSE_MTGO, borrower_id=borrower,
        card_name=name, quantity=qty, created_by="test", source_id=f"t:{borrower}:{name}")


async def test_expected_stock_is_owned_minus_out_across_every_library(test_db):
    await a_library("lounge", stock={"Lightning Bolt": 4, "Counterspell": 2})
    await a_library("cube-night", guild="g2", stock={"Lightning Bolt": 1})
    await _lend("lounge", "p1", "Lightning Bolt", 3)
    await _lend("lounge", "p2", "Counterspell", 2)

    expected = await inventory_reconcile.expected_library_stock()

    assert expected == {"Lightning Bolt": 2}


async def test_card_audit_is_clean_when_the_shelf_matches(test_db):
    await a_library("lounge", stock={"Lightning Bolt": 4})
    await _lend("lounge", "p1", "Lightning Bolt", 1)
    serve = FakeLendingServe(stock={"Lightning Bolt": 3, "Island": 10})

    audit = await inventory_reconcile.audit_library_cards(serve)

    assert audit.verdict == CLEAN
    assert audit.extra == [CardLine("Island", 0, 10)], "extras are reported, not a mismatch"


async def test_card_audit_names_what_is_missing(test_db):
    await a_library("lounge", stock={"Lightning Bolt": 4, "Counterspell": 2})
    serve = FakeLendingServe(stock={"Lightning Bolt": 1, "Counterspell": 2})

    audit = await inventory_reconcile.audit_library_cards(serve)

    assert audit.verdict == MISMATCH
    assert audit.missing == [CardLine("Lightning Bolt", 4, 1)]
    assert "3x Lightning Bolt" in inventory_reconcile.describe_cards(audit)


async def test_a_moving_loan_makes_a_difference_unsettled(test_db):
    from database.db_session import AsyncSessionLocal
    from models.card_loan import CardLoan

    await a_library("lounge", stock={"Lightning Bolt": 4})
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id="g1", library_id="lounge", borrower_id="p1",
                       cards=[{"name": "Lightning Bolt", "qty": 4}], state="out_pending"))
        await s.commit()
    serve = FakeLendingServe(stock={})

    audit = await inventory_reconcile.audit_library_cards(serve)

    assert audit.verdict == UNSETTLED
    assert [f.kind for f in audit.in_flight] == ["out_pending"]


async def test_an_inconsistent_reading_is_unsettled(test_db):
    await a_library("lounge", stock={"Lightning Bolt": 4})

    audit = await inventory_reconcile.audit_library_cards(
        FakeLendingServe(stock={}, consistent=False))

    assert audit.verdict == UNSETTLED


async def test_no_inventory_is_unreadable_never_all_missing(test_db):
    await a_library("lounge", stock={"Lightning Bolt": 4})

    audit = await inventory_reconcile.audit_library_cards(
        FakeLendingServe(stock={}, inventory_route=False))

    assert audit.verdict == UNREADABLE
    assert audit.missing == []


# ---------------------------------------------------------------------------
# the tix audit
# ---------------------------------------------------------------------------
async def _wallet(amount, job_id):
    await wallet_service.credit_done("g1", "p1", amount, job_id=job_id)


async def test_tix_audit_balances_and_mismatches(test_db):
    await _wallet(30, "job-a")

    assert (await inventory_reconcile.audit_tix(FakeLendingServe(tix=30))).verdict == CLEAN
    short = await inventory_reconcile.audit_tix(FakeLendingServe(tix=25))
    assert short.verdict == MISMATCH
    assert short.diff == -5


async def test_tix_audit_lists_unbooked_jobs(test_db):
    from database.db_session import AsyncSessionLocal
    from models.mtgo_job import MtgoJob

    async with AsyncSessionLocal() as s:
        s.add(MtgoJob(job_id="j1", kind="deposit", guild_id="g1", player_id="p1",
                      mtgo_user="someone", amount=10, status="pending"))
        await s.commit()

    audit = await inventory_reconcile.audit_tix(FakeLendingServe(tix=10))

    assert audit.verdict == UNSETTLED
    assert [f.ref for f in audit.in_flight] == ["j1"]


async def test_disabled_serve_is_unreadable(test_db):
    audit = await inventory_reconcile.audit_tix(SimpleNamespace(enabled=False))

    assert audit.verdict == UNREADABLE


# ---------------------------------------------------------------------------
# stock checks
# ---------------------------------------------------------------------------
async def test_available_now_reads_an_unlisted_card_as_zero(test_db, monkeypatch):
    serve = FakeLendingServe(stock={"Lightning Bolt": 2})
    monkeypatch.setattr(card_lending_service, "get_lending_client", lambda: serve)

    stock = await card_lending_service.available_now(["Lightning Bolt", "Black Lotus"])

    assert stock == {"Lightning Bolt": 2, "Black Lotus": 0}


async def test_an_inconsistent_inventory_does_not_vouch_for_absence(test_db, monkeypatch):
    """A snapshot from before a running trade may predate a return: its zero is
    not evidence, so the asked name is left unlisted, the lenient reading."""
    serve = FakeLendingServe(stock={"Lightning Bolt": 2}, consistent=False)
    monkeypatch.setattr(card_lending_service, "get_lending_client", lambda: serve)

    stock = await card_lending_service.available_now(["Lightning Bolt", "Black Lotus"])

    assert stock == {"Lightning Bolt": 2}


async def test_a_promise_draws_down_the_stack_whatever_its_spelling(test_db, monkeypatch):
    from database.db_session import AsyncSessionLocal
    from models.card_loan import CardLoan

    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id="g1", library_id="lounge", borrower_id="p1",
                       cards=[{"name": "lightning bolt", "qty": 2}], state="out_pending"))
        await s.commit()
    serve = FakeLendingServe(stock={"Lightning Bolt": 3})
    monkeypatch.setattr(card_lending_service, "get_lending_client", lambda: serve)

    stock = await card_lending_service.available_now(["Lightning Bolt"])

    assert stock == {"Lightning Bolt": 1}


async def test_two_spellings_of_one_card_share_one_stack(test_db, monkeypatch):
    serve = FakeLendingServe(stock={"Lightning Bolt": 2})
    monkeypatch.setattr(card_lending_service, "get_lending_client", lambda: serve)

    stock = await card_lending_service.available_now(["Lightning Bolt", "lightning bolt"])

    assert stock["Lightning Bolt"] + stock["lightning bolt"] == 2


async def test_available_now_falls_back_to_the_truncated_vault(test_db, monkeypatch):
    serve = FakeLendingServe(stock={"Lightning Bolt": 2}, inventory_route=False)
    monkeypatch.setattr(card_lending_service, "get_lending_client", lambda: serve)

    stock = await card_lending_service.available_now(["Lightning Bolt", "Black Lotus"])

    assert stock == {"Lightning Bolt": 2}, "absent from /vault means unlisted, not zero"


async def test_lendable_now_caps_by_what_is_physically_there(test_db, monkeypatch):
    await a_library("lounge", stock={"Lightning Bolt": 4, "Counterspell": 2})
    serve = FakeLendingServe(stock={"Lightning Bolt": 1})
    monkeypatch.setattr(card_lending_service, "get_lending_client", lambda: serve)

    assert await card_lending_service.lendable_now("lounge") == {
        "Lightning Bolt": 1, "Counterspell": 0}
