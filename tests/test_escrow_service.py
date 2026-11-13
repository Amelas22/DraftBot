import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from database.db_session import db_session
from models.debt_ledger import DebtLedger
from models.escrow_hold import OPEN, RELEASED, RETURNED
from services import escrow_service, wallet_service
import notification_service
from services.escrow_service import EscrowAlreadyDecided, EscrowNotFound
from services.wallet_service import InsufficientFunds, escrow_holder_id

G = "g"


async def bal(player: str) -> int:
    return await wallet_service.get_balance(G, player)


async def fund(player: str, amount: int) -> None:
    await wallet_service.credit_done(G, player, amount, job_id=f"seed-{player}")


@pytest.fixture
def notes():
    """The three outbound calls, replaced so no DM goes anywhere."""
    with patch("services.escrow_service.on_inflow", new=AsyncMock()) as inflow, \
            patch("notification_service.notify_wallet", new=AsyncMock()) as wallet, \
            patch("notification_service.notify_escrow_opened", new=AsyncMock()), \
            patch("notification_service.notify_escrow_decided", new=AsyncMock()):
        yield inflow, wallet


async def opened(amount: int = 60):
    await fund("A", 100)
    return await escrow_service.open_escrow(G, "A", "B", amount, "tix sale")


@pytest.mark.asyncio
async def test_open_moves_the_tix_out_of_the_funders_wallet(test_db, notes):
    e = await opened()
    assert await bal("A") == 40
    assert await bal(escrow_holder_id(e.id)) == 60
    assert await bal("B") == 0
    assert e.status == OPEN


@pytest.mark.asyncio
async def test_open_notifies_both_parties_without_paying_anyone(test_db, notes):
    inflow, wallet = notes
    await opened()
    assert wallet.await_count == 2
    assert {c.args[2] for c in wallet.await_args_list} == {"A", "B"}
    inflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_refuses_more_than_the_balance_and_leaves_no_record(test_db, notes):
    await fund("A", 10)
    with pytest.raises(InsufficientFunds) as x:
        await escrow_service.open_escrow(G, "A", "B", 60, "tix sale")
    assert x.value.available == 10
    assert await escrow_service.open_escrows(G) == []
    assert await bal("A") == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("kw", [dict(amount=0), dict(counterparty_id="A"), dict(note="  ")])
async def test_open_rejects_bad_input(test_db, notes, kw):
    await fund("A", 100)
    args = dict(guild_id=G, funder_id="A", counterparty_id="B", amount=60, note="n")
    args.update(kw)
    with pytest.raises(ValueError):
        await escrow_service.open_escrow(**args)
    assert await bal("A") == 100
    assert await escrow_service.open_escrows(G) == []


@pytest.mark.asyncio
async def test_a_note_over_the_limit_is_refused_and_books_nothing(test_db, notes):
    await fund("A", 100)
    with pytest.raises(ValueError):
        await escrow_service.open_escrow(G, "A", "B", 10, "x" * (escrow_service.NOTE_MAX + 1))
    assert await bal("A") == 100
    assert await escrow_service.open_escrows(G) == []


@pytest.mark.asyncio
async def test_a_note_at_the_limit_is_accepted(test_db, notes):
    await fund("A", 100)
    e = await escrow_service.open_escrow(G, "A", "B", 10, "x" * escrow_service.NOTE_MAX)
    assert e.note == "x" * escrow_service.NOTE_MAX
    assert await bal(escrow_holder_id(e.id)) == 10


@pytest.mark.asyncio
async def test_release_pays_the_counterparty_in_full(test_db, notes):
    inflow, wallet = notes
    e = await opened()
    out = await escrow_service.decide(G, e.id, RELEASED, "MOD")
    assert await bal("B") == 60
    assert await bal(escrow_holder_id(e.id)) == 0
    assert await bal("A") == 40
    assert (out.status, out.decided_by) == (RELEASED, "MOD")
    assert out.decided_at is not None
    inflow.assert_awaited_once()
    assert inflow.await_args.args[:2] == (G, "B")
    assert inflow.await_args.args[2] is notification_service.notify_escrow_decided
    assert wallet.await_args.args[2] == "A"
    for call in (inflow.await_args, wallet.await_args):
        assert call.kwargs["note"] == "tix sale"


@pytest.mark.asyncio
async def test_return_pays_the_funder_back(test_db, notes):
    inflow, _ = notes
    e = await opened()
    await escrow_service.decide(G, e.id, RETURNED, "MOD")
    assert await bal("A") == 100
    assert await bal("B") == 0
    assert inflow.await_args.args[:2] == (G, "A")


@pytest.mark.asyncio
async def test_an_unknown_outcome_is_refused(test_db, notes):
    e = await opened()
    with pytest.raises(ValueError):
        await escrow_service.decide(G, e.id, OPEN, "MOD")
    assert await bal(escrow_holder_id(e.id)) == 60


@pytest.mark.asyncio
async def test_deciding_twice_is_refused_and_moves_nothing(test_db, notes):
    e = await opened()
    await escrow_service.decide(G, e.id, RELEASED, "MOD")
    with pytest.raises(EscrowAlreadyDecided) as x:
        await escrow_service.decide(G, e.id, RETURNED, "MOD2")
    assert x.value.escrow.status == RELEASED
    assert x.value.escrow.decided_by == "MOD"
    assert x.value.escrow.id == e.id
    assert await bal("B") == 60
    assert await bal("A") == 40


@pytest.mark.asyncio
async def test_two_mods_at_once_only_one_wins(test_db, notes, monkeypatch):
    # An asyncio.Lock binds to a loop the first time it is contended, and each
    # test runs on its own loop. This is the test that contends it on purpose,
    # so it takes a lock of its own rather than one an earlier test bound.
    monkeypatch.setattr(wallet_service, "MONEY_LOCK", asyncio.Lock())
    e = await opened()
    results = await asyncio.gather(
        escrow_service.decide(G, e.id, RELEASED, "M1"),
        escrow_service.decide(G, e.id, RETURNED, "M2"),
        return_exceptions=True)
    assert sum(isinstance(r, EscrowAlreadyDecided) for r in results) == 1
    holder = await bal(escrow_holder_id(e.id))
    assert holder == 0
    assert await bal("A") + await bal("B") + holder == 100


@pytest.mark.asyncio
async def test_an_escrow_from_another_server_is_not_found(test_db, notes):
    e = await opened()
    with pytest.raises(EscrowNotFound):
        await escrow_service.decide("other-guild", e.id, RELEASED, "MOD")
    with pytest.raises(EscrowNotFound):
        await escrow_service.decide(G, e.id + 99, RELEASED, "MOD")
    assert await bal(escrow_holder_id(e.id)) == 60


@pytest.mark.asyncio
async def test_a_mod_can_decide_an_escrow_they_are_party_to(test_db, notes):
    e = await opened()
    out = await escrow_service.decide(G, e.id, RELEASED, "B")
    assert out.status == RELEASED


@pytest.mark.asyncio
async def test_system_total_is_unchanged_by_open_and_decide(test_db, notes):
    await fund("A", 100)
    before = await wallet_service.total_wallets()
    e = await escrow_service.open_escrow(G, "A", "B", 60, "n")
    assert await wallet_service.total_wallets() == before
    await escrow_service.decide(G, e.id, RELEASED, "MOD")
    assert await wallet_service.total_wallets() == before


@pytest.mark.asyncio
async def test_a_failing_notifier_does_not_undo_the_decision(test_db):
    with patch("services.escrow_service.on_inflow",
               new=AsyncMock(side_effect=RuntimeError("boom"))), \
            patch("notification_service.notify_wallet",
                  new=AsyncMock(side_effect=RuntimeError("boom"))), \
            patch("notification_service.notify_escrow_opened", new=AsyncMock()):
        e = await opened()
        out = await escrow_service.decide(G, e.id, RELEASED, "MOD")
    assert out.status == RELEASED
    assert await bal("B") == 60


@pytest.mark.asyncio
async def test_get_escrow_finds_an_escrow_of_any_status_in_its_own_guild(test_db, notes):
    e = await opened()
    assert (await escrow_service.get_escrow(G, e.id)).status == OPEN
    await escrow_service.decide(G, e.id, RELEASED, "MOD")
    assert (await escrow_service.get_escrow(G, e.id)).status == RELEASED


@pytest.mark.asyncio
async def test_get_escrow_does_not_find_another_guilds_or_a_missing_escrow(test_db, notes):
    e = await opened()
    assert await escrow_service.get_escrow("other-guild", e.id) is None
    assert await escrow_service.get_escrow(G, e.id + 100) is None


@pytest.mark.asyncio
async def test_open_escrows_lists_only_this_guilds_open_ones_oldest_first(test_db, notes):
    await fund("A", 100)
    first = await escrow_service.open_escrow(G, "A", "B", 10, "1")
    second = await escrow_service.open_escrow(G, "A", "B", 10, "2")
    third = await escrow_service.open_escrow(G, "A", "B", 10, "3")
    await escrow_service.decide(G, second.id, RELEASED, "MOD")
    await wallet_service.credit_done("h", "A", 50, job_id="seed-h")
    await escrow_service.open_escrow("h", "A", "B", 10, "other guild")

    listed = await escrow_service.open_escrows(G)
    assert [e.id for e in listed] == [first.id, third.id]


@pytest.mark.asyncio
async def test_escrows_for_includes_both_roles_and_caps_decided_at_the_limit(test_db, notes):
    await fund("A", 100)
    await fund("B", 100)
    await fund("C", 100)
    mine_open = await escrow_service.open_escrow(G, "A", "B", 5, "A funds")
    theirs_open = await escrow_service.open_escrow(G, "B", "A", 5, "A is counterparty")
    await escrow_service.open_escrow(G, "B", "C", 5, "not A's")
    decided = []
    for i in range(4):
        e = await escrow_service.open_escrow(G, "A", "B", 1, f"old {i}")
        await escrow_service.decide(G, e.id, RELEASED, "MOD")
        decided.append(e.id)

    rows = await escrow_service.escrows_for(G, "A", decided_limit=2)
    open_ids = {r.id for r in rows if r.status == OPEN}
    decided_ids = {r.id for r in rows if r.status != OPEN}
    assert open_ids == {mine_open.id, theirs_open.id}
    assert decided_ids == set(decided[-2:])


def test_recipient_of():
    class E:
        funder_id, counterparty_id = "A", "B"
    assert escrow_service.recipient_of(E(), RELEASED) == "B"  # type: ignore[arg-type]
    assert escrow_service.recipient_of(E(), RETURNED) == "A"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_release_settles_what_the_recipient_owes_a_third_player(test_db):
    """The real on_inflow, unpatched: with no bot registered the DMs are no-ops, but the
    debt settlement runs. Real players are numeric Discord ids -- settle_inflow skips
    anything else as a synthetic holder. The timeout is the deadlock guard: on_inflow
    takes MONEY_LOCK, so calling it from inside the lock would hang here."""
    funder, recipient, creditor = "101", "202", "303"
    await fund(funder, 100)
    async with db_session() as session:
        for player, other, amount in ((recipient, creditor, -20), (creditor, recipient, 20)):
            session.add(DebtLedger(guild_id=G, player_id=player, counterparty_id=other,
                                   amount=amount, source_type="manual", source_id="d1",
                                   created_by=recipient))
    e = await escrow_service.open_escrow(G, funder, recipient, 60, "tix sale")

    released = await asyncio.wait_for(
        escrow_service.decide(G, e.id, RELEASED, "MOD"), timeout=10)

    assert released.status == RELEASED
    assert await bal(creditor) == 20
    assert await bal(recipient) == 40
    assert await bal(escrow_holder_id(e.id)) == 0
