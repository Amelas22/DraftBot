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
import asyncio

from loguru import logger
from sqlalchemy import func, select, update

from database.db_session import db_session
from models.debt_ledger import DebtLedger
from models.draft_session import DraftSession
from models.stake import StakeInfo
from services import debt_service, wallet_service
from services.debt_service import TIX_ONLY
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


DONATION_SOURCE = "donation"

# Serializes the check-then-insert in book_donation_debts, for the reason
# debt_service._STAKE_DEBT_LOCK gives: settlement runs inside every match
# report, so two can overlap, and the bot is the database's only writer.
_DONATION_LOCK = asyncio.Lock()


def donation_source_id(session_id: str, player_id: str) -> str:
    """The idempotency key of one loser's donation debt."""
    return f"{DONATION_SOURCE}:{session_id}:{player_id}"


def draftbot_id() -> str | None:
    """DraftBot's own user id, read from the running bot -- never hard-coded,
    so a dev bot collects in dev. None until the bot has logged in."""
    from bot_registry import get_bot

    user = getattr(get_bot(), "user", None)
    return str(user.id) if user is not None else None


async def book_donation_debts(guild_id: str, session_id: str, winners: list[str],
                              losers: list[str], bot_id: str) -> list[str]:
    """Book each loser's share as a debt to DraftBot. Returns who THIS call booked.

    Books once per draft: any existing donation row for it means a replay or a
    corrected result, and nothing more is booked. Shares come from
    team creation; a draft a crash left without them is levelled here from the
    declared entries rather than settling for nothing.
    """
    shares = await donation_shares(session_id)
    if not shares:
        logger.warning(f"donation draft {session_id}: no shares recorded; "
                       f"levelling from the declared entries now")
        shares = (await level_donation(session_id, winners, losers))["held"]

    booked: list[str] = []
    async with _DONATION_LOCK, db_session() as session:
        already = (await session.execute(
            select(DebtLedger.id).where(
                DebtLedger.guild_id == guild_id,
                DebtLedger.source_type == DONATION_SOURCE,
                DebtLedger.source_id.like(f"{DONATION_SOURCE}:{session_id}:%"))
            .limit(1))).first()
        if already is not None:
            # The first decision owns the money. Keys are per loser, so a
            # correction that flips the result would otherwise book the other
            # team too and collect from both sides.
            logger.warning(f"donation draft {session_id}: already booked; a corrected "
                           f"result does not move donation money")
            return []
        for loser in losers:
            amount = shares.get(loser, 0)
            key = donation_source_id(session_id, loser)
            if amount <= 0:
                continue
            note = f"Donation draft {session_id}: share owed to DraftBot"
            session.add(DebtLedger(guild_id=guild_id, player_id=loser,
                                   counterparty_id=bot_id, amount=-amount,
                                   source_type=DONATION_SOURCE, source_id=key, notes=note))
            session.add(DebtLedger(guild_id=guild_id, player_id=bot_id,
                                   counterparty_id=loser, amount=amount,
                                   source_type=DONATION_SOURCE, source_id=key, notes=note))
            booked.append(loser)
    if booked:
        logger.info(f"donation draft {session_id}: {booked} owe DraftBot their shares")
    return booked


async def donation_debtors(guild_id: str, session_id: str) -> list[str]:
    """Everyone this draft made a debtor of DraftBot, settled or not.

    Read back from the ledger, not from what booking returned, so every later
    match report is a free retry of a settlement that failed once -- the same
    reason debt_service.get_draft_debtors exists.
    """
    async with db_session() as session:
        rows = await session.execute(
            select(DebtLedger.player_id).where(
                DebtLedger.guild_id == guild_id,
                DebtLedger.source_type == DONATION_SOURCE,
                DebtLedger.source_id.like(f"{DONATION_SOURCE}:{session_id}:%"),
                DebtLedger.amount < 0,
                TIX_ONLY,
            ).distinct())
        return list(rows.scalars().all())


async def settle_donation_draft(guild_id: str, session_id: str, outcome: str,
                                winners: list[str], losers: list[str]) -> list[str]:
    """Move a decided donation draft's money: losers owe DraftBot, funded ones pay now.

    A draw books nothing. The draw from a loser's wallet is the ordinary
    settle_new_debts pass, so it notifies exactly as any debt does.
    """
    if outcome == "draw":
        return []
    bot_id = draftbot_id()
    if bot_id is None:
        # Booking against anyone else would be wrong and permanent; the next
        # match report runs this again.
        logger.error(f"donation draft {session_id}: DraftBot's id is not known "
                     f"yet; nothing booked")
        return []
    booked = await book_donation_debts(guild_id, session_id, winners, losers, bot_id)

    from services.mtgo_resolution_service import settle_new_debts
    await settle_new_debts(guild_id, await donation_debtors(guild_id, session_id))
    return booked


def _by_share(item: tuple[str, int]) -> int:
    return item[1]


async def current_team_shares(session_id: str) -> dict[str, int]:
    """donation_shares, less anyone no longer on a team.

    A player removed or subbed out after teams formed keeps their StakeInfo
    row; embeds must neither list them nor count their share.
    """
    shares = await donation_shares(session_id)
    async with db_session() as session:
        teams = (await session.execute(
            select(DraftSession.team_a, DraftSession.team_b)
            .where(DraftSession.session_id == session_id))).first()
    members = {str(p) for team in (teams or ()) for p in (team or [])}
    return {p: n for p, n in shares.items() if p in members}


async def format_donation_entries(session_id: str,
                                  sign_ups: dict[str, str]) -> tuple[list[str], int]:
    """Each player's share, for the teams embed: what they owe DraftBot if their side loses."""
    shares = await current_team_shares(session_id)
    ranked = sorted(shares.items(), key=_by_share, reverse=True)
    lines = [f"**{sign_ups.get(p, 'Unknown')}**: {n} tix" for p, n in ranked]
    return lines, sum(shares.values())


async def format_donation_outcomes(session_id: str, sign_ups: dict[str, str],
                                   winning_team: list[str]) -> tuple[list[str], int]:
    """What the result means, for the victory embed. The total is what goes to DraftBot.

    Once the draft's debts are booked the ledger is the truth (the first decision
    stands, even if the result is later corrected), so the booked debtors are the
    losers and everyone else on a team won. `winning_team` is only used before
    anything is booked.
    """
    shares = await current_team_shares(session_id)
    winners = set(winning_team)
    async with db_session() as session:
        guild_id = (await session.execute(
            select(DraftSession.guild_id)
            .where(DraftSession.session_id == session_id))).scalars().first()
    if guild_id is not None:
        debtors = set(await donation_debtors(str(guild_id), session_id))
        if debtors:
            winners = set(shares) - debtors
    lines: list[str] = []
    donated = 0
    for player_id, share in sorted(shares.items(), key=_by_share, reverse=True):
        name = sign_ups.get(player_id, "Unknown")
        if player_id in winners:
            lines.append(f"**{name}** won {share} tix for DraftBot")
        else:
            lines.append(f"**{name}** donates {share} tix to DraftBot")
            donated += share
    return lines, donated


async def _donation_outstanding(guild_id: str, player_id: str, bot_id: str) -> int:
    """What this player still owes DraftBot from donation drafts.

    Every donation debt they were booked, less every credit they hold against
    DraftBot -- settlements, transfers, admin credits, whatever the source. Credits
    are not tagged with the debt they pay, so all of them count against donations
    first -- which can only UNDER-state the figure, so a cash mark can never reach
    a debt that is not a donation.
    """
    async with db_session() as session:
        pledged = (await session.execute(
            select(func.coalesce(func.sum(-DebtLedger.amount), 0)).where(
                DebtLedger.guild_id == guild_id, DebtLedger.player_id == player_id,
                DebtLedger.counterparty_id == bot_id,
                DebtLedger.source_type == DONATION_SOURCE, TIX_ONLY))).scalar()
        settled = (await session.execute(
            select(func.coalesce(func.sum(DebtLedger.amount), 0)).where(
                DebtLedger.guild_id == guild_id, DebtLedger.player_id == player_id,
                DebtLedger.counterparty_id == bot_id,
                DebtLedger.amount > 0, TIX_ONLY))).scalar()
    return max(int(pledged or 0) - int(settled or 0), 0)


async def mark_paid_cash(guild_id: str, player_id: str, bot_id: str) -> int:
    """Settle the caller's own donation debt to DraftBot as paid in cash, on trust.

    Never another player's debt, and never a debt that is not a donation. A
    repeat finds nothing outstanding and settles nothing. Held under MONEY_LOCK
    so it cannot interleave with an auto-draw or a second click: both re-read
    what is owed, and the second finds it already settled.
    """
    async with wallet_service.MONEY_LOCK:
        owed = -await debt_service.get_balance_with(guild_id, player_id, bot_id)
        amount = min(owed, await _donation_outstanding(guild_id, player_id, bot_id))
        if amount <= 0:
            return 0
        await debt_service.create_settlement(
            guild_id, player_id, bot_id, amount, settled_by=player_id,
            settlement_method="cash")
    logger.info(f"donation: {player_id} marked {amount} tix paid to DraftBot in cash")
    return amount


def stakes_field_name(donation: bool, total: int) -> str:
    """The teams/victory/live embeds' stakes field label: a donation draft has no pool."""
    return f"**{'Donation stakes' if donation else 'Prize Pool'}: {total} tix**"


def payouts_title(donation: bool) -> str:
    return "💜 Donations to DraftBot" if donation else "💰 Prize Pool Payouts"


def payouts_footer(donation: bool, total: int) -> str:
    return (f"Donated to DraftBot: {total} tix" if donation
            else f"Winnings paid: {total} tix")


SIGNUP_DONATION_NOTE = (
    "Nothing leaves your wallet now. If your side loses, you owe DraftBot your "
    "matched entry: it is drawn from your wallet automatically, or pay cash and "
    "run `/donation_day paid_cash`.")
