"""Entry fees as pure transfers into the tournament's prize wallet.

A pending registration survives a failed/absent trade window and completes whenever the
tix arrive by any route; the fee then lives in the prize wallet (not a hold on the
captain), and dropping the team transfers it back.
"""
import pytest
from sqlalchemy import select

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.tournament import Tournament, TournamentParticipant
from models.wallet_tx import WalletTx
from services import tournament_escrow_service as escrow
from services import wallet_service

GUILD = "g1"
CAPTAIN = "cap1"
CREDITOR = "cred1"


async def _paid_tournament(fee=2, status="registration", team="Pending Squad"):
    async with db_session() as session:
        t = Tournament(guild_id=GUILD, name="Fee Cup", total_rounds=0, format="manual",
                       status=status, entry_fee=fee)
        session.add(t)
        await session.flush()
        p = TournamentParticipant(tournament_id=t.id, team_id=1, team_name=team,
                                  captain_user_id=CAPTAIN, status="pending")
        session.add(p)
        await session.flush()
        return t.id, p.id


async def _status(participant_id):
    async with db_session() as session:
        p = await session.get(TournamentParticipant, participant_id)
        return p.status if p else None


@pytest.mark.asyncio
async def test_sweep_leaves_entry_pending_while_wallet_is_short(test_db):  # noqa: F811
    t_id, p_id = await _paid_tournament(fee=2)
    await wallet_service.credit_done(GUILD, CAPTAIN, 1, job_id="j-short")

    assert await escrow.sweep_pending_entries() == []
    assert await _status(p_id) == "pending"
    # nothing moved: the tix are wholly the captain's, the pot is empty
    assert await wallet_service.get_balance(GUILD, CAPTAIN) == 1
    assert await escrow.prize_pool(GUILD, t_id) == 0


@pytest.mark.asyncio
async def test_fee_transfers_into_the_prize_wallet_when_funds_arrive(test_db):  # noqa: F811
    t_id, p_id = await _paid_tournament(fee=2)
    assert await escrow.sweep_pending_entries() == []  # no funds yet, spot held

    await wallet_service.credit_done(GUILD, CAPTAIN, 3, job_id="j-late")
    assert await escrow.sweep_pending_entries() == [t_id]
    assert await _status(p_id) == "paid"

    # the fee LEFT the captain and now belongs to the pot — no hold, no status
    assert await wallet_service.get_balance(GUILD, CAPTAIN) == 1
    assert await escrow.prize_pool(GUILD, t_id) == 2
    async with db_session() as session:
        rows = (await session.execute(
            select(WalletTx).where(WalletTx.source == escrow.escrow_source(t_id, p_id))
        )).scalars().all()
    assert sorted(r.amount for r in rows) == [-2, 2]  # one transfer pair, nets to zero


@pytest.mark.asyncio
async def test_sweep_is_idempotent_and_charges_once(test_db):  # noqa: F811
    t_id, p_id = await _paid_tournament(fee=2)
    await wallet_service.credit_done(GUILD, CAPTAIN, 4, job_id="j-plenty")

    assert await escrow.sweep_pending_entries() == [t_id]
    assert await escrow.sweep_pending_entries() == []
    # re-securing an already-paid entry must not charge a second fee
    res = await escrow.secure_from_wallet(GUILD, CAPTAIN, p_id, t_id, 2, "Pending Squad")
    assert res["done"] and res.get("reused")
    assert await wallet_service.get_balance(GUILD, CAPTAIN) == 2
    assert await escrow.prize_pool(GUILD, t_id) == 2


@pytest.mark.asyncio
async def test_committed_fee_cannot_be_spent_on_a_debt_or_pay(test_db):  # noqa: F811
    """The fee is gone from the captain's balance, so ordinary spending simply can't reach it."""
    t_id, p_id = await _paid_tournament(fee=2)
    await wallet_service.credit_done(GUILD, CAPTAIN, 2, job_id="j-exact")
    await escrow.sweep_pending_entries()

    assert await wallet_service.get_balance(GUILD, CAPTAIN) == 0
    with pytest.raises(ValueError):
        await wallet_service.pay(GUILD, CAPTAIN, CREDITOR, 1, source="nope")
    assert await escrow.prize_pool(GUILD, t_id) == 2


@pytest.mark.asyncio
async def test_dropping_a_team_refunds_the_fee_out_of_the_pot(test_db):  # noqa: F811
    t_id, p_id = await _paid_tournament(fee=2, team="Droppers")
    await wallet_service.credit_done(GUILD, CAPTAIN, 2, job_id="j-drop")
    await escrow.sweep_pending_entries()
    assert await escrow.prize_pool(GUILD, t_id) == 2

    res = await escrow.drop_with_refund(t_id, "Droppers")
    assert res["refunded"] == 2
    assert await wallet_service.get_balance(GUILD, CAPTAIN) == 2  # whole again
    assert await escrow.prize_pool(GUILD, t_id) == 0
    assert await _status(p_id) is None  # participant removed


@pytest.mark.asyncio
async def test_sweep_ignores_tournaments_past_registration(test_db):  # noqa: F811
    """Once a tournament starts, a still-unpaid team is out — the sweep must not
    quietly seat (and charge) it afterwards."""
    t_id, p_id = await _paid_tournament(fee=2, status="active")
    await wallet_service.credit_done(GUILD, CAPTAIN, 5, job_id="j-late2")

    assert await escrow.sweep_pending_entries() == []
    assert await _status(p_id) == "pending"
    assert await wallet_service.get_balance(GUILD, CAPTAIN) == 5  # not charged
    assert await escrow.prize_pool(GUILD, t_id) == 0


@pytest.mark.asyncio
async def test_ledger_nets_to_zero_across_entry_and_refund(test_db):  # noqa: F811
    """Transfers never change the system total — the reconciliation invariant by
    construction. Only the boundary credit (the deposit) moves it."""
    t_id, _ = await _paid_tournament(fee=2, team="Netters")
    await wallet_service.credit_done(GUILD, CAPTAIN, 2, job_id="j-net")
    assert await wallet_service.total_wallets() == 2

    await escrow.sweep_pending_entries()
    assert await wallet_service.total_wallets() == 2  # fee moved, total unchanged

    await escrow.drop_with_refund(t_id, "Netters")
    assert await wallet_service.total_wallets() == 2  # refund moved it back


# --- board freshness while an entry is still short -----------------------------

@pytest.mark.asyncio
async def test_a_partial_deposit_leaves_the_board_listed_but_completes_nothing(test_db):  # noqa: F811
    """The exact case that showed a stale deficit: 1 tix toward a 2 tix fee."""
    t_id, p_id = await _paid_tournament(fee=2, team="Short Squad")
    await wallet_service.credit_done(GUILD, CAPTAIN, 1, job_id="j-partial")

    assert await escrow.sweep_pending_entries(CAPTAIN) == []      # nothing completed
    assert await escrow.open_boards_for_captain(CAPTAIN) == [t_id]  # ...but still needs a refresh
    assert await escrow.open_boards_for_captain("someone-else") == []  # scoped to the captain
    assert await _status(p_id) == "pending"
    assert await wallet_service.get_balance(GUILD, CAPTAIN) == 1   # partial stays liquid


@pytest.mark.asyncio
async def test_a_paid_entry_drops_off_the_open_boards_list(test_db):  # noqa: F811
    t_id, _ = await _paid_tournament(fee=2, team="Payers")
    await wallet_service.credit_done(GUILD, CAPTAIN, 2, job_id="j-full")

    assert await escrow.sweep_pending_entries(CAPTAIN) == [t_id]
    assert await escrow.open_boards_for_captain(CAPTAIN) == []


@pytest.mark.asyncio
async def test_open_registration_boards_is_the_watchdog_backstop(test_db):  # noqa: F811
    """The watchdog re-renders these every tick, which is what keeps a board honest
    after balance changes no refresh call site covers (/wallet pay, a withdraw, a debt
    settlement) — so it must list open tournaments regardless of fee or paid state."""
    open_paid, _ = await _paid_tournament(fee=2, team="Still Open")
    async with db_session() as session:
        free = Tournament(guild_id=GUILD, name="Free Cup", total_rounds=0, format="manual",
                          status="registration", entry_fee=0)
        started = Tournament(guild_id=GUILD, name="Started Cup", total_rounds=0,
                             format="manual", status="active", entry_fee=2)
        session.add_all([free, started])
        await session.flush()
        free_id, started_id = free.id, started.id

    boards = await escrow.open_registration_boards()

    assert open_paid in boards and free_id in boards   # every open board gets re-rendered
    assert started_id not in boards                    # a started tournament is frozen
