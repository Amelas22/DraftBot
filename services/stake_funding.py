"""Can this player take on another draft's risk?

The invariant, checked wherever a stake is declared:

    balance >= what you already owe
             + the most you could lose from drafts you are already in
             + the stake you are declaring now

It is a precondition on taking on risk rather than a lock on the wallet. A
player can still withdraw after joining, so this guarantees solvency at the
moment of commitment, not at settlement -- but that is the guarantee that makes
moving to real escrow safe, because everyone already in a draft is known to
have been able to cover it.

Obligations do not net. Being owed money by one player does not pay a debt to
another, and a draft you might win is not income you can stake elsewhere: both
sides of that coin are already handled by counting only what could be LOST.
"""
from loguru import logger
from sqlalchemy import select

from database.db_session import db_session
from models.draft_session import DraftSession
from models.stake import StakeInfo
from services import debt_service, wallet_service

# A draft still capable of costing its players money. NULL is the queue.
_RESOLVED = ("completed", "abandoned")


async def potential_losses(guild_id: str, player_id: str,
                           exclude_session_id: str | None = None) -> dict[str, int]:
    """The most this player could still lose, per unfinished draft.

    Keyed by session id so a caller can say which drafts are holding the money.
    A player's declared stake is the ceiling on what a draft can cost them,
    which is what makes this a sound bound rather than an estimate.

    Membership comes from the draft's sign_ups, not from the StakeInfo row:
    leaving a draft does not delete that row, so trusting its existence would
    hold a player's money hostage to a draft they walked away from.
    """
    async with db_session() as session:
        rows = (await session.execute(
            select(StakeInfo.session_id, StakeInfo.max_stake, DraftSession.sign_ups)
            .join(DraftSession, DraftSession.session_id == StakeInfo.session_id)
            .where(
                StakeInfo.player_id == player_id,
                DraftSession.guild_id == guild_id,
                DraftSession.session_type == "staked",
                DraftSession.session_stage.is_(None)
                | DraftSession.session_stage.notin_(_RESOLVED),
            ))).all()

    at_risk: dict[str, int] = {}
    for session_id, max_stake, sign_ups in rows:
        if session_id == exclude_session_id:
            continue        # the draft being declared for; the new figure replaces it
        if player_id not in (sign_ups or {}):
            continue        # a row left behind by leaving
        at_risk[session_id] = max(int(max_stake or 0), 0)
    return at_risk


async def obligations(guild_id: str, player_id: str,
                      exclude_session_id: str | None = None) -> tuple[int, int]:
    """(owed_now, could_still_lose) -- everything already claimed against a wallet."""
    owed = (await debt_service.get_total_owed_map(guild_id, [player_id])).get(player_id, 0)
    at_risk = await potential_losses(guild_id, player_id, exclude_session_id)
    return int(owed), sum(at_risk.values())


async def shortfall(guild_id: str, player_id: str, session_id: str,
                    stake: int) -> int:
    """How many more tix this player needs to declare `stake`. 0 when funded.

    Returns the gap rather than a bool so the caller can tell them what to
    deposit instead of only that they were refused.
    """
    owed, at_risk = await obligations(guild_id, player_id, exclude_session_id=session_id)
    balance = await wallet_service.get_balance(guild_id, player_id)
    gap = (owed + at_risk + int(stake)) - balance
    if gap > 0:
        logger.info(
            f"stake funding: {player_id} short {gap} tix for {stake} on "
            f"{session_id} (holds {balance}, owes {owed}, at risk {at_risk})")
    return max(gap, 0)
