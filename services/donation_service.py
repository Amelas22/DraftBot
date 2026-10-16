"""Donation drafts: staked drafts whose losers owe DraftBot instead of paying winners.

A donation draft escrows nothing. Sign-up charges nothing (draft_pool_service.
entry_in), team creation levels the DECLARED entries with the same rule a prize
pool uses and records each player's share in StakeInfo.assigned_stake, and
settlement books each loser's share as a debt to DraftBot -- which the ordinary
debt machinery then auto-draws from any loser who holds tix.

DraftBot is only ever the CREDITOR here. Its id is all digits, so wallet code
treats it as a person, and a debt booked against it would auto-draw from the
donations it holds.
"""
from loguru import logger
from sqlalchemy import update, select

from database.db_session import db_session
from models.stake import StakeInfo
from services.draft_pool_service import MatchResult, declared_bets, plan_levelling


async def level_donation(session_id: str, team_a: list[str],
                         team_b: list[str]) -> MatchResult:
    """Level a donation draft's declared entries and record each share.

    The same plan match_pool books for a prize pool, but nothing is refunded
    because nothing was taken: a player's share is simply written down, and it
    is what they owe DraftBot if their side loses. Idempotent: the declared
    entries are frozen once the queue closes, so a replay writes the same
    shares.
    """
    bets, wants_cap = await declared_bets(session_id)
    entries = {p: int(n) for p, n in bets.items() if n and int(n) > 0}
    plan = plan_levelling(team_a, team_b, entries, bets, wants_cap)

    async with db_session() as session:
        for player_id in [*team_a, *team_b]:
            await session.execute(
                update(StakeInfo)
                .where(StakeInfo.session_id == session_id,
                       StakeInfo.player_id == player_id)
                .values(assigned_stake=plan["held"].get(player_id, 0)))

    logger.info(f"donation draft {session_id}: levelled at {plan['matched']} a side")
    return plan


async def donation_shares(session_id: str) -> dict[str, int]:
    """Each player's recorded share of a donation draft, zero shares left out."""
    async with db_session() as session:
        rows = (await session.execute(
            select(StakeInfo.player_id, StakeInfo.assigned_stake)
            .where(StakeInfo.session_id == session_id))).all()
    return {player_id: int(share) for player_id, share in rows if share}
