"""Debt settlement out of the tix wallet, and the auto-draw that runs after a deposit.

These paths had no coverage, which is how a stale `status=` kwarg survived the move to
the status-free ledger and broke every settlement at runtime.
"""
import asyncio

import pytest
from sqlalchemy import select

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.debt_ledger import DebtLedger
from services import debt_service
from services import mtgo_resolution_service as resolution
from services import wallet_service as ws

GUILD = "g1"
PAYER = "111111111111111111"
CRED = "222222222222222222"
CRED2 = "333333333333333333"


async def _owe(payer, creditor, amount, source_id):
    """Book a debt: payer owes creditor ``amount``."""
    async with db_session() as session:
        session.add(DebtLedger(guild_id=GUILD, player_id=payer, counterparty_id=creditor,
                               amount=-amount, source_type="manual", source_id=source_id,
                               created_by=payer))
        session.add(DebtLedger(guild_id=GUILD, player_id=creditor, counterparty_id=payer,
                               amount=amount, source_type="manual", source_id=source_id,
                               created_by=payer))


@pytest.mark.asyncio
async def test_settlement_moves_tix_and_clears_the_debt(test_db):  # noqa: F811
    await ws.credit_done(GUILD, PAYER, 5, job_id="j1")
    await _owe(PAYER, CRED, 3, "d1")

    res = await resolution.settle_debt_from_wallet(GUILD, PAYER, CRED, 3)
    assert res["ok"]
    assert await ws.get_balance(GUILD, PAYER) == 2
    assert await ws.get_balance(GUILD, CRED) == 3
    # the wallet move is a transfer, so the vault total is untouched
    assert await ws.total_wallets() == 5
    balances = await debt_service.get_all_balances_for(GUILD, PAYER)
    assert balances.get(CRED, 0) == 0


@pytest.mark.asyncio
async def test_settlement_is_idempotent_by_link_id(test_db):  # noqa: F811
    await ws.credit_done(GUILD, PAYER, 5, job_id="j2")
    await _owe(PAYER, CRED, 3, "d2")

    first = await resolution.settle_debt_from_wallet(GUILD, PAYER, CRED, 3, link_id="link-1")
    again = await resolution.settle_debt_from_wallet(GUILD, PAYER, CRED, 3, link_id="link-1")
    assert first["ok"] and again["ok"]
    assert await ws.get_balance(GUILD, PAYER) == 2  # charged once


@pytest.mark.asyncio
async def test_settlement_refuses_more_than_the_wallet_or_the_debt(test_db):  # noqa: F811
    await ws.credit_done(GUILD, PAYER, 2, job_id="j3")
    await _owe(PAYER, CRED, 10, "d3")

    broke = await resolution.settle_debt_from_wallet(GUILD, PAYER, CRED, 5)
    assert not broke["ok"] and "insufficient" in broke["error"].lower()

    await ws.credit_done(GUILD, PAYER, 20, job_id="j3b")
    too_much = await resolution.settle_debt_from_wallet(GUILD, PAYER, CRED, 15)
    assert not too_much["ok"] and "exceeds" in too_much["error"]
    assert await ws.get_balance(GUILD, PAYER) == 22  # nothing moved


@pytest.mark.asyncio
async def test_auto_draw_applies_a_deposit_to_debts_oldest_first(test_db):  # noqa: F811
    await _owe(PAYER, CRED, 2, "d4")
    await _owe(PAYER, CRED2, 2, "d5")
    await ws.credit_done(GUILD, PAYER, 3, job_id="j4")

    drawn = await resolution.auto_draw(GUILD, PAYER)
    assert sum(d["amount"] for d in drawn) == 3  # spent the whole balance
    assert await ws.get_balance(GUILD, PAYER) == 0
    assert await ws.get_balance(GUILD, CRED) + await ws.get_balance(GUILD, CRED2) == 3
    assert await ws.total_wallets() == 3


@pytest.mark.asyncio
async def test_auto_draw_is_a_no_op_without_funds_or_debts(test_db):  # noqa: F811
    assert await resolution.auto_draw(GUILD, PAYER) == []      # no funds, no debts
    await ws.credit_done(GUILD, PAYER, 4, job_id="j5")
    assert await resolution.auto_draw(GUILD, PAYER) == []      # funds, no debts
    assert await ws.get_balance(GUILD, PAYER) == 4


# ---- settling on every inflow, not just deposits --------------------------------

@pytest.mark.asyncio
async def test_a_payment_settles_the_recipients_debt_on_the_way_in(test_db):  # noqa: F811
    """The point of the change: a debtor paid by another player has that money applied
    to what they owe, instead of it landing spendable and the creditor staying unpaid."""
    await ws.credit_done(GUILD, CRED2, 5, job_id="j1")
    await _owe(PAYER, CRED, 3, "d1")

    res = await resolution.pay(GUILD, CRED2, PAYER, 5)

    assert res["ok"]
    assert [d["amount"] for d in res["settled"]] == [3]
    assert await ws.get_balance(GUILD, PAYER) == 2   # 5 in, 3 straight out to the creditor
    assert await ws.get_balance(GUILD, CRED) == 3


@pytest.mark.asyncio
async def test_a_returned_withdraw_settles_on_the_way_back(test_db):  # noqa: F811
    """Tix coming back from a failed withdraw are an inflow like any other."""
    await ws.credit_done(GUILD, PAYER, 5, job_id="j1")
    await ws.pay(GUILD, PAYER, ws.SYSTEM_IN_FLIGHT, 5, source="wd:test", notes="withdraw")
    await _owe(PAYER, CRED, 3, "d1")
    assert await ws.get_balance(GUILD, PAYER) == 0   # nothing to draw against yet

    await resolution._return_in_flight(GUILD, PAYER, 5, "test")

    assert await ws.get_balance(GUILD, CRED) == 3
    assert await ws.get_balance(GUILD, PAYER) == 2


@pytest.mark.asyncio
async def test_settle_inflow_skips_system_accounts(test_db):  # noqa: F811
    """Prize wallets and in-flight hold claims but owe nothing; drawing against them
    is meaningless, and would let a synthetic holder be treated as a debtor."""
    await ws.credit_done(GUILD, PAYER, 5, job_id="j1")
    await ws.pay(GUILD, PAYER, ws.SYSTEM_IN_FLIGHT, 5, source="wd:test", notes="withdraw")

    assert await resolution.settle_inflow(GUILD, ws.SYSTEM_IN_FLIGHT) == []


@pytest.mark.asyncio
async def test_settle_inflow_never_breaks_the_inflow(test_db, monkeypatch):  # noqa: F811
    """The money has already moved by the time this runs. A settlement failure must not
    turn a completed transfer into an error -- which is also why the deposit path calls
    settle_inflow: a raise there would abort before the user's confirmation is sent."""
    async def boom(*a, **kw):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(resolution, "auto_draw", boom)

    assert await resolution.settle_inflow(GUILD, PAYER) == []


@pytest.mark.asyncio
async def test_a_deposit_pays_the_players_own_entry_before_their_creditors(test_db):  # noqa: F811
    """Order matters, and the amounts here make the two orders disagree: owing 5 with a
    2-tix entry pending and 5 arriving, entry-first completes the entry and pays the
    creditor 3, while debt-first hands the creditor all 5 and strands the entry.

    The player committed to that entry, so it has first claim on funds they just put in.
    """
    from models.tournament import Tournament, TournamentParticipant
    from database.db_session import db_session as _db

    async with _db() as session:
        t = Tournament(guild_id=GUILD, name="Fee Cup", total_rounds=0, format="manual",
                       status="registration", entry_fee=2)
        session.add(t)
        await session.flush()
        session.add(TournamentParticipant(tournament_id=t.id, team_id=1, team_name="Alpha",
                                          captain_user_id=PAYER, status="pending"))
    await _owe(PAYER, CRED, 5, "d1")
    await ws.credit_done(GUILD, PAYER, 5, job_id="j-dep")

    completed, drawn = await resolution.settle_deposit_inflow(GUILD, PAYER)

    assert completed, "the pending entry should have been completed first"
    assert await ws.get_balance(GUILD, CRED) == 3      # creditor got only what was left
    assert await ws.get_balance(GUILD, PAYER) == 0
    assert sum(d["amount"] for d in drawn) == 3


# ---- a new debt draws against the wallet the moment it appears -------------------

@pytest.mark.asyncio
async def test_a_new_debt_is_debited_from_the_wallet_immediately(test_db):  # noqa: F811
    """Losing a staked draft while holding tix must not leave paying up to the debtor's
    discretion. Settlement used to fire only when money arrived; a debt arriving at
    someone who ALREADY has funds is the mirror case, and it went uncollected."""
    await ws.credit_done(GUILD, PAYER, 10, job_id="j1")
    await _owe(PAYER, CRED, 4, "d1")          # the stake outcome lands

    drawn = await resolution.settle_new_debts(GUILD, [PAYER])

    assert sum(d["amount"] for d in drawn) == 4
    balances = await debt_service.get_all_balances_for(GUILD, PAYER)
    assert balances.get(CRED, 0) == 0
    assert await ws.get_balance(GUILD, PAYER) == 6
    assert await ws.get_balance(GUILD, CRED) == 4


@pytest.mark.asyncio
async def test_a_new_debt_takes_what_it_can_when_the_wallet_is_short(test_db):  # noqa: F811
    """Partial funds still collect. Leaving a short wallet untouched would hand back the
    very discretion this removes -- the remainder simply stays owed."""
    await ws.credit_done(GUILD, PAYER, 3, job_id="j1")
    await _owe(PAYER, CRED, 10, "d1")

    drawn = await resolution.settle_new_debts(GUILD, [PAYER])

    assert sum(d["amount"] for d in drawn) == 3
    assert await ws.get_balance(GUILD, PAYER) == 0
    balances = await debt_service.get_all_balances_for(GUILD, PAYER)
    assert balances.get(CRED, 0) == -7, "the unpaid remainder must stay on the books"


@pytest.mark.asyncio
async def test_a_new_debt_against_an_empty_wallet_is_a_clean_no_op(test_db):  # noqa: F811
    await _owe(PAYER, CRED, 10, "d1")

    assert await resolution.settle_new_debts(GUILD, [PAYER]) == []
    balances = await debt_service.get_all_balances_for(GUILD, PAYER)
    assert balances.get(CRED, 0) == -10


@pytest.mark.asyncio
async def test_stake_outcomes_are_debited_as_soon_as_they_are_created(test_db):  # noqa: F811
    """The slice as the draft-completion path runs it: create the stake debts, then hand
    the debtors straight to settlement. Losing with a funded wallet settles on the spot."""
    from models.stake_pairing import StakePairing
    from services.debt_service import create_debt_entries_from_stakes

    async with db_session() as session:
        session.add(StakePairing(session_id="s-1", player_a_id=PAYER,
                                 player_b_id=CRED, amount=4))
    await ws.credit_done(GUILD, PAYER, 10, job_id="j1")

    debts = await create_debt_entries_from_stakes(
        guild_id=GUILD, session_id="s-1", winning_team_ids=[CRED])
    drawn = await resolution.settle_new_debts(GUILD, [debtor for debtor, _, _ in debts])

    assert debts == [(PAYER, CRED, 4)]
    assert sum(d["amount"] for d in drawn) == 4
    assert await ws.get_balance(GUILD, PAYER) == 6
    assert await ws.get_balance(GUILD, CRED) == 4
    balances = await debt_service.get_all_balances_for(GUILD, PAYER)
    assert balances.get(CRED, 0) == 0


@pytest.mark.asyncio
async def test_racing_renders_create_one_set_of_stake_debts(test_db):  # noqa: F811
    """The idempotency check is SELECT-then-insert with no DB uniqueness guard, and the
    embed that runs it is re-rendered from six call sites. Two racing renders could each
    see no rows and both insert -- and once a debt is auto-debited, a duplicate is money
    genuinely taken twice rather than a stray row someone can delete."""
    from models.stake_pairing import StakePairing
    from services.debt_service import create_debt_entries_from_stakes

    async with db_session() as session:
        session.add(StakePairing(session_id="race-1", player_a_id=PAYER,
                                 player_b_id=CRED, amount=4))

    both = await asyncio.gather(
        create_debt_entries_from_stakes(guild_id=GUILD, session_id="race-1",
                                        winning_team_ids=[CRED]),
        create_debt_entries_from_stakes(guild_id=GUILD, session_id="race-1",
                                        winning_team_ids=[CRED]),
    )

    assert sorted(len(r) for r in both) == [0, 1], "exactly one call may create the debts"
    async with db_session() as session:
        rows = (await session.execute(
            select(DebtLedger).where(DebtLedger.source_id == "race-1"))).scalars().all()
    assert len(rows) == 2, "one debtor row and one creditor row, not four"


@pytest.mark.asyncio
async def test_an_uncollected_stake_debt_is_collected_on_a_later_pass(test_db):  # noqa: F811
    """Deriving the debtors from create_debt_entries_from_stakes' return value makes
    settlement one-shot: a replay returns [] so nobody is revisited, and a debtor whose
    settlement failed -- or who funds their wallet a minute later -- is never collected.
    Reading the debtors back off the ledger turns every later pass into a retry."""
    from models.stake_pairing import StakePairing
    from services.debt_service import create_debt_entries_from_stakes

    async with db_session() as session:
        session.add(StakePairing(session_id="s-2", player_a_id=PAYER,
                                 player_b_id=CRED, amount=4))

    debts = await create_debt_entries_from_stakes(
        guild_id=GUILD, session_id="s-2", winning_team_ids=[CRED])
    assert await resolution.settle_new_debts(
        GUILD, [d for d, _, _ in debts]) == [], "empty wallet: nothing to collect yet"

    await ws.credit_done(GUILD, PAYER, 10, job_id="j1")   # funded after the fact

    replay = await create_debt_entries_from_stakes(
        guild_id=GUILD, session_id="s-2", winning_team_ids=[CRED])
    assert replay == [], "the creator is idempotent, so it can't name the debtors again"

    debtors = await debt_service.get_draft_debtors(GUILD, "s-2")
    drawn = await resolution.settle_new_debts(GUILD, debtors)

    assert sum(d["amount"] for d in drawn) == 4
    assert await ws.get_balance(GUILD, PAYER) == 6


# ---- paying someone you owe settles that debt ------------------------------------

@pytest.mark.asyncio
async def test_paying_a_creditor_settles_that_debt(test_db):  # noqa: F811
    """Reported live: keezles owed Birb 10, sent 10 with /wallet pay, and the debt stayed
    on the books until Birb cleared it by hand. Settlement fired only for the RECIPIENT's
    debts (on_inflow), so a debtor paying their creditor directly was the one route that
    skipped it -- while merely RECEIVING tix would have settled the very same debt."""
    await ws.credit_done(GUILD, PAYER, 10, job_id="j1")
    await _owe(PAYER, CRED, 4, "d1")

    res = await resolution.pay(GUILD, PAYER, CRED, 4)

    assert res["ok"]
    balances = await debt_service.get_all_balances_for(GUILD, PAYER)
    assert balances.get(CRED, 0) == 0, "the debt should be cleared, not just the tix moved"
    assert await ws.get_balance(GUILD, PAYER) == 6
    assert await ws.get_balance(GUILD, CRED) == 4
    assert await ws.total_wallets() == 10, "the tix must move exactly once, not twice"


@pytest.mark.asyncio
async def test_paying_more_than_the_debt_settles_it_and_sends_the_rest(test_db):  # noqa: F811
    """Overpaying stays one command: the debt closes and the surplus lands as an ordinary
    payment, so the creditor ends up with the whole amount either way."""
    await ws.credit_done(GUILD, PAYER, 10, job_id="j1")
    await _owe(PAYER, CRED, 3, "d1")

    res = await resolution.pay(GUILD, PAYER, CRED, 8)

    assert res["ok"]
    assert res["debt_settled"]["amount"] == 3
    balances = await debt_service.get_all_balances_for(GUILD, PAYER)
    assert balances.get(CRED, 0) == 0
    assert await ws.get_balance(GUILD, CRED) == 8      # 3 settling the debt + 5 plain
    assert await ws.get_balance(GUILD, PAYER) == 2
    assert await ws.total_wallets() == 10


@pytest.mark.asyncio
async def test_paying_someone_you_do_not_owe_is_still_a_plain_transfer(test_db):  # noqa: F811
    """Regression guard: debt-awareness must not disturb the ordinary case, including the
    recipient's own auto-draw on the way in."""
    await ws.credit_done(GUILD, PAYER, 10, job_id="j1")
    await _owe(CRED, CRED2, 2, "d1")          # the RECIPIENT owes someone else

    res = await resolution.pay(GUILD, PAYER, CRED, 5)

    assert res["ok"] and res["debt_settled"] is None
    assert [d["amount"] for d in res["settled"]] == [2]   # recipient's debt drawn as before
    assert await ws.get_balance(GUILD, PAYER) == 5
    assert await ws.get_balance(GUILD, CRED) == 3         # 5 in, 2 out to their creditor
    assert await ws.get_balance(GUILD, CRED2) == 2


# ---- on_inflow: the one call every inflow path makes ----------------------------

@pytest.mark.asyncio
async def test_on_inflow_announces_then_settles(test_db):  # noqa: F811
    """One function owns the whole concept, so a new inflow path cannot half-implement
    it: the recipient is told, THEN the money is applied to their debts — that order is
    what makes their two DMs read as "received", then "auto-applied"."""
    import notification_service
    from unittest.mock import AsyncMock, patch

    await ws.credit_done(GUILD, PAYER, 5, job_id="j1")
    await _owe(PAYER, CRED, 3, "d1")
    order = []

    async def fake_send(*a, **kw):
        order.append("announced")
        return True

    real_settle = resolution.settle_inflow

    async def watched_settle(*a, **kw):
        order.append("settled")
        return await real_settle(*a, **kw)

    with patch.object(notification_service, "send_dm", new=AsyncMock(side_effect=fake_send)), \
         patch("bot_registry.get_bot", return_value=object()), \
         patch.object(resolution, "settle_inflow", new=watched_settle):
        drawn = await resolution.on_inflow(
            GUILD, PAYER, notification_service.notify_payment_received,
            CRED2, 5, note="test")

    # settling fires its own DMs (the auto-settlement pair), so "announced" recurs
    # after "settled" -- what matters is that the inflow was announced FIRST.
    assert order[0] == "announced"
    assert order.index("settled") == 1
    assert sum(d["amount"] for d in drawn) == 3
    assert await ws.get_balance(GUILD, CRED) == 3


@pytest.mark.asyncio
async def test_on_inflow_without_an_announcement_still_settles(test_db):  # noqa: F811
    """Paths where the recipient already sees the outcome (a returned withdraw, a
    deposit's own confirmation) pass no notifier and must still settle."""
    await ws.credit_done(GUILD, PAYER, 5, job_id="j1")
    await _owe(PAYER, CRED, 3, "d1")

    drawn = await resolution.on_inflow(GUILD, PAYER)

    assert sum(d["amount"] for d in drawn) == 3
