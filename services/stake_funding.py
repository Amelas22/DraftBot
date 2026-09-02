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
from typing import TypedDict

from loguru import logger
from sqlalchemy import select

from database.db_session import db_session
from models.draft_session import DraftSession
from helpers.stale_drafts import is_finished_draft
from models.stake import StakeInfo
from services import debt_service, wallet_service

# A draft still capable of costing its players money. NULL is the queue.
_RESOLVED = ("completed", "abandoned")


async def already_satisfied(guild_id: str, session_id: str, player_id: str) -> int:
    """How much of this draft's stake the player has already made good on.

    Paying an entry into the prize pool satisfies the obligation it stands for
    -- the tix are no longer promised, they are delivered. Zero for a draft
    that predates the pool, which is right for the same reason: nothing was
    delivered, so the whole declared stake is still owed to it.
    """
    from services.draft_pool_service import held_by

    return await held_by(guild_id, session_id, player_id)


async def potential_losses(guild_id: str, player_id: str,
                           exclude_session_id: str | None = None) -> dict[str, int]:
    """What this player still owes to unfinished drafts, per draft.

    Keyed by session id so a caller can say which drafts are holding the money.
    A player's declared stake is the ceiling on what a draft can cost them,
    which is what makes this a sound bound rather than an estimate.

    Membership comes from the draft's sign_ups, not from the StakeInfo row:
    leaving a draft does not delete that row, so trusting its existence would
    hold a player's money hostage to a draft they walked away from.
    """
    async with db_session() as session:
        rows = (await session.execute(
            select(StakeInfo.session_id, StakeInfo.max_stake, DraftSession.sign_ups,
                   DraftSession.session_stage,
                   DraftSession.victory_message_id_draft_chat,
                   DraftSession.victory_message_id_results_channel)
            .join(DraftSession, DraftSession.session_id == StakeInfo.session_id)
            .where(
                StakeInfo.player_id == player_id,
                DraftSession.guild_id == guild_id,
                DraftSession.session_type == "staked",
                # A prefilter only. It can exclude a draft that is definitely
                # over ('abandoned' has no victory message to find), never keep
                # one -- is_finished_draft below is the authority.
                DraftSession.session_stage.is_(None)
                | DraftSession.session_stage.notin_(_RESOLVED),
            ))).all()

    at_risk: dict[str, int] = {}
    for row in rows:
        session_id, max_stake, sign_ups = row.session_id, row.max_stake, row.sign_ups
        stage = row.session_stage
        if session_id == exclude_session_id:
            continue        # the draft being declared for; the new figure replaces it
        if player_id not in (sign_ups or {}):
            continue        # a row left behind by leaving
        if is_finished_draft(row):
            # The stage is not a record of completion. helpers/stale_drafts puts
            # it plainly -- it "rarely advances past 'pairings' even for fully
            # played drafts" -- and nothing wrote 'completed' at all before
            # 2026-01, so every staked draft older than that still reads as live.
            # A posted victory message is the durable evidence, and ledger_stats
            # already treats it that way. Believing the stage alone reserved a
            # player's whole staking history against their wallet forever.
            continue
        # Only the UNSATISFIED part of a promise is still a claim on the wallet.
        # Escrowing an entry is not a reduction to net out, it is the obligation
        # being met: those tix have been handed over and the balance already
        # reflects it.
        met = await already_satisfied(guild_id, session_id, player_id)
        if met and stage is not None:
            # An escrowed draft whose book has closed. No more money can go in,
            # and matching has already handed back whatever it could not match,
            # so the entry is settled at what is held and the wallet reflects
            # both halves. Reserving the declared figure on top would charge
            # the player for tix already sitting back in their balance.
            continue
        if not met:
            # Nothing was ever charged, so this is a pre-conversion draft that
            # will settle as debt. The whole declaration is still a future
            # liability however far the draft has got.
            at_risk[session_id] = max(int(max_stake or 0), 0)
            continue
        at_risk[session_id] = max(int(max_stake or 0) - met, 0)
    return at_risk


async def obligations(guild_id: str, player_id: str,
                      exclude_session_id: str | None = None) -> tuple[int, int]:
    """(owed_now, could_still_lose) -- everything already claimed against a wallet."""
    owed = (await debt_service.get_total_owed_map(guild_id, [player_id])).get(player_id, 0)
    at_risk = await potential_losses(guild_id, player_id, exclude_session_id)
    return int(owed), sum(at_risk.values())


class Funding(TypedDict):
    """Why a stake was affordable or not, from one pass over the ledger."""
    gap: int            # tix still needed; 0 when the stake is affordable
    balance: int        # what the wallet holds now
    owed: int           # debt already on the books
    at_risk: int        # what other unfinished drafts still claim
    already_in: int     # what is already paid into THIS draft


async def shortfall(guild_id: str, player_id: str, session_id: str,
                    stake: int) -> Funding:
    """What it would take for this player to declare `stake`.

    Returns the whole picture rather than a bool, so a caller can tell them
    what to deposit and why -- reading it back out of the ledger a second time
    to build that message would ask the same four questions twice.

    What they have already paid into THIS draft counts toward the figure they
    are declaring. Escrow makes a revision a top-up rather than a fresh
    purchase: 50 into a draft and raising to 100 owes the pool 50, not 100.
    Charging the whole stake again would ask them to fund it twice out of a
    wallet the first payment had just emptied.
    """
    owed, at_risk = await obligations(guild_id, player_id, exclude_session_id=session_id)
    already = await already_satisfied(guild_id, session_id, player_id)
    balance = await wallet_service.get_balance(guild_id, player_id)
    still_owed_here = max(int(stake) - already, 0)
    gap = (owed + at_risk + still_owed_here) - balance
    if gap > 0:
        logger.info(
            f"stake funding: {player_id} short {gap} tix for {stake} on "
            f"{session_id} (holds {balance}, already in {already}, owes {owed}, "
            f"at risk {at_risk})")
    return {"gap": max(gap, 0), "balance": balance, "owed": owed,
            "at_risk": at_risk, "already_in": already}
