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


# ---- payout identifies the tournament by name, not just id ----------------------

@pytest.mark.asyncio
async def test_payout_reports_the_tournament_name(test_db):  # noqa: F811
    """execute_payout knew the tournament only as an integer id, so its log line and its
    result could not say which event paid. Callers that report a payout to a human — the
    operator reading logs, and a prize DM — need the name."""
    t_id, _ = await _paid_tournament(fee=2, status="active", team="Roto Chaff")
    await wallet_service.credit_done(GUILD, CAPTAIN, 2, job_id="j-pool")
    await escrow.secure_from_wallet(GUILD, CAPTAIN, 1, t_id, 2, "Roto Chaff")

    res = await escrow.execute_payout(GUILD, t_id, [(1, CAPTAIN, "Roto Chaff", 2)])

    assert res["ok"]
    assert res["tournament_name"] == "Fee Cup"


@pytest.mark.asyncio
async def test_payout_name_is_absent_when_the_row_cannot_be_read(test_db):  # noqa: F811
    """The name is reporting, not money: if the row can't be read the payout still pays.

    Nothing deletes a Tournament today, so this reaches the None branch the only way it
    can be reached — by removing the row, which the test database permits because FK
    enforcement is off. It pins the defensive behaviour, not a race that happens."""
    t_id, _ = await _paid_tournament(fee=2, status="active")
    await wallet_service.credit_done(GUILD, CAPTAIN, 2, job_id="j-pool2")
    await escrow.secure_from_wallet(GUILD, CAPTAIN, 1, t_id, 2, "Pending Squad")
    async with db_session() as session:
        await session.delete(await session.get(Tournament, t_id))

    res = await escrow.execute_payout(GUILD, t_id, [(1, CAPTAIN, "Pending Squad", 2)])

    assert res["ok"]
    assert res["tournament_name"] is None


# --- dropping one team must not disturb any other team's payment ----------------
# The escrow once hung a mutable status and an escrow_tx_id off the participant, so a
# drop reached into ledger rows and could perturb a *different* team's payment state.
# b668a76 replaced that with plain transfers; nothing asserted the multi-team property
# it was supposed to buy, so these pin it down.

async def _team(t_id, team, captain, team_id):
    async with db_session() as session:
        p = TournamentParticipant(tournament_id=t_id, team_id=team_id, team_name=team,
                                  captain_user_id=captain, status="pending")
        session.add(p)
        await session.flush()
        return p.id


async def _participant(participant_id):
    async with db_session() as session:
        return await session.get(TournamentParticipant, participant_id)


@pytest.mark.asyncio
async def test_dropping_one_team_leaves_the_others_paid_and_the_pot_intact(test_db):  # noqa: F811
    """A drop touches exactly one team's money and nobody else's."""
    t_id, a_id = await _paid_tournament(fee=2, team="Team A")
    b_id = await _team(t_id, "Team B", "cap-b", 2)
    c_id = await _team(t_id, "Team C", "cap-c", 3)
    for captain, job in ((CAPTAIN, "j-a"), ("cap-b", "j-b"), ("cap-c", "j-c")):
        await wallet_service.credit_done(GUILD, captain, 2, job_id=job)
    await escrow.sweep_pending_entries()
    assert await escrow.prize_pool(GUILD, t_id) == 6

    before_a, before_c = await _participant(a_id), await _participant(c_id)
    assert (before_a.status, before_c.status) == ("paid", "paid")

    res = await escrow.drop_with_refund(t_id, "Team B")

    assert res["refunded"] == 2
    assert await _status(b_id) is None                      # B is gone
    assert await wallet_service.get_balance(GUILD, "cap-b") == 2   # B made whole

    after_a, after_c = await _participant(a_id), await _participant(c_id)
    assert (after_a.status, after_c.status) == ("paid", "paid")
    assert (after_a.paid_at, after_c.paid_at) == (before_a.paid_at, before_c.paid_at)
    assert await wallet_service.get_balance(GUILD, CAPTAIN) == 0   # A's fee still committed
    assert await wallet_service.get_balance(GUILD, "cap-c") == 0
    assert await escrow.prize_pool(GUILD, t_id) == 4               # only B's fee left
    assert await wallet_service.total_wallets() == 6               # transfers net to zero


@pytest.mark.asyncio
async def test_dropping_the_same_team_twice_refunds_once(test_db):  # noqa: F811
    """The second drop must not pay the captain a second time out of the pot."""
    t_id, _ = await _paid_tournament(fee=2, team="Twice")
    await wallet_service.credit_done(GUILD, CAPTAIN, 2, job_id="j-twice")
    await escrow.sweep_pending_entries()

    assert (await escrow.drop_with_refund(t_id, "Twice"))["refunded"] == 2
    balance_after_first = await wallet_service.get_balance(GUILD, CAPTAIN)

    with pytest.raises(ValueError):
        await escrow.drop_with_refund(t_id, "Twice")

    assert await wallet_service.get_balance(GUILD, CAPTAIN) == balance_after_first
    assert await escrow.prize_pool(GUILD, t_id) == 0
    assert await wallet_service.total_wallets() == 2


# --- /tournament start refuses while anyone is unpaid ---------------------------
# remove_team is registration-only, so a start that merely warned would flip the
# tournament to 'active' and strand the unpaid rows with no way to clear them. The
# start has to refuse, leaving the TO in the phase where they can still act.

async def _swiss_registration(fee=2):
    """A swiss tournament with two paid teams and one that never paid."""
    async with db_session() as session:
        t = Tournament(guild_id=GUILD, name="Gate Cup", total_rounds=3, format="swiss",
                       status="registration", entry_fee=fee)
        session.add(t)
        await session.flush()
        t_id = t.id
    for team, captain, team_id in (("Paid One", CAPTAIN, 1), ("Paid Two", "cap-2", 2),
                                   ("Skint", "cap-3", 3)):
        await _team(t_id, team, captain, team_id)
    for captain, job in ((CAPTAIN, "j-g1"), ("cap-2", "j-g2")):
        await wallet_service.credit_done(GUILD, captain, fee, job_id=job)
    await escrow.sweep_pending_entries()
    return t_id


async def _round_count(t_id):
    async with db_session() as session:
        from models.tournament import TournamentRound
        rows = (await session.execute(
            select(TournamentRound).where(TournamentRound.tournament_id == t_id))).scalars().all()
        return len(rows)


@pytest.mark.asyncio
async def test_start_refuses_while_a_team_has_not_paid(test_db):  # noqa: F811
    import random
    t_id = await _swiss_registration()

    with pytest.raises(ValueError, match="Skint"):
        await escrow.close_registration_and_seed(GUILD, random.Random(1))

    async with db_session() as session:
        assert (await session.get(Tournament, t_id)).status == "registration"
    assert await _round_count(t_id) == 0


@pytest.mark.asyncio
async def test_start_proceeds_once_the_unpaid_team_is_dropped(test_db):  # noqa: F811
    """The TO's actual workflow: told who is short, drop them, start again."""
    import random
    t_id = await _swiss_registration()
    await escrow.drop_with_refund(t_id, "Skint")

    res = await escrow.close_registration_and_seed(GUILD, random.Random(1))

    assert res["tournament_id"] == t_id
    async with db_session() as session:
        assert (await session.get(Tournament, t_id)).status == "active"
    assert await _round_count(t_id) == 1
