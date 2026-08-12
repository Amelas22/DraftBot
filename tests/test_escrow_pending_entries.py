"""A pending entry-fee registration survives a failed trade window and completes
whenever the tix arrive — by any route (retried deposit, plain /wallet deposit,
a teammate's /wallet pay). See tournament_escrow_service.sweep_pending_entries.
"""
import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.tournament import Tournament, TournamentParticipant
from services import tournament_escrow_service as escrow
from services import wallet_service

GUILD = "g1"
CAPTAIN = "cap1"


async def _paid_tournament(fee=2, status="registration"):
    """A tournament with an entry fee plus one team pending escrow."""
    async with db_session() as session:
        t = Tournament(guild_id=GUILD, name="Fee Cup", total_rounds=0, format="manual",
                       status=status, entry_fee=fee)
        session.add(t)
        await session.flush()
        p = TournamentParticipant(tournament_id=t.id, team_id=1, team_name="Pending Squad",
                                  captain_user_id=CAPTAIN, status="pending")
        session.add(p)
        await session.flush()
        return t.id, p.id


async def _status(participant_id):
    async with db_session() as session:
        return (await session.get(TournamentParticipant, participant_id)).status


@pytest.mark.asyncio
async def test_sweep_leaves_entry_pending_while_wallet_is_short(test_db):  # noqa: F811
    _, p_id = await _paid_tournament(fee=2)
    await wallet_service.credit_done(GUILD, CAPTAIN, 1, kind="deposit", job_id="j-short")

    assert await escrow.sweep_pending_entries() == 0
    assert await _status(p_id) == "pending"
    # the short balance is untouched — nothing was half-reserved
    assert await wallet_service.get_available(GUILD, CAPTAIN) == 1


@pytest.mark.asyncio
async def test_sweep_completes_entry_once_funds_arrive(test_db):  # noqa: F811
    _, p_id = await _paid_tournament(fee=2)
    # first trade window failed: nothing credited, spot still pending
    assert await escrow.sweep_pending_entries() == 0
    assert await _status(p_id) == "pending"

    # tix arrive later by any route
    await wallet_service.credit_done(GUILD, CAPTAIN, 2, kind="deposit", job_id="j-late")

    assert await escrow.sweep_pending_entries() == 1
    assert await _status(p_id) == "paid"
    # the fee is held (reserved), not spent
    assert await wallet_service.get_balance(GUILD, CAPTAIN) == 2
    assert await wallet_service.get_available(GUILD, CAPTAIN) == 0


@pytest.mark.asyncio
async def test_sweep_is_idempotent_and_holds_the_fee_once(test_db):  # noqa: F811
    _, p_id = await _paid_tournament(fee=2)
    await wallet_service.credit_done(GUILD, CAPTAIN, 4, kind="deposit", job_id="j-plenty")

    assert await escrow.sweep_pending_entries() == 1
    assert await escrow.sweep_pending_entries() == 0  # already paid: nothing to do
    assert await _status(p_id) == "paid"
    assert await wallet_service.get_available(GUILD, CAPTAIN) == 2  # exactly one 2-tix hold


@pytest.mark.asyncio
async def test_sweep_ignores_tournaments_past_registration(test_db):  # noqa: F811
    """Once a tournament starts, a still-unpaid team is out — the sweep must not
    quietly seat (and charge) it afterwards."""
    _, p_id = await _paid_tournament(fee=2, status="active")
    await wallet_service.credit_done(GUILD, CAPTAIN, 5, kind="deposit", job_id="j-late2")

    assert await escrow.sweep_pending_entries() == 0
    assert await _status(p_id) == "pending"
    assert await wallet_service.get_available(GUILD, CAPTAIN) == 5  # not charged
