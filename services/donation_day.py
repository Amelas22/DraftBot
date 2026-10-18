"""Donation day: one guild's event totals and leaderboard, for its public page.

Credits BOTH sides of a donation draft, and the double count is intended:
winners are credited what they WON for DraftBot (their matched share), losers
what they have actually PAID. The headline "raised" counts money once -- what
was paid, by tix or by a cash self-mark, plus direct /wallet pay gifts to
DraftBot since the event started. "Pledged" adds what losers still owe.

Never emits a Discord id: a player is a resolved display name, or "Anonymous".
"""
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, TypedDict

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_config, update_setting
from database.db_session import db_session
from models.debt_ledger import DebtLedger
from models.draft_session import DraftSession
from models.stake import StakeInfo
from models.wallet_tx import TIX, WalletTx
from preference_service import get_anonymous_donors
from services.debt_service import TIX_ONLY
from services.donation_service import DONATION_SOURCE
from services.league_site_data import resolve_display_names
from services.wallet_service import is_system_account

ANONYMOUS = "Anonymous"
UNNAMED = "A player"        # never fall back to an id
RECENT_LIMIT = 12


class Event(TypedDict):
    name: str
    goal_usd: int
    starts_at: str
    page_key: str
    active: bool


@dataclass(frozen=True)
class Credit:
    player_id: str
    amount: int
    at: datetime
    kind: str               # "won" | "paid"
    key: str = ""           # stable identity, so a credit is announced once
    method: str = ""        # paid credits: "wallet" | "cash" | "external" | "gift"
    draft: str = ""         # win credits: the draft's friendly_id, else its session_id


@dataclass(frozen=True)
class Snapshot:
    credits: list[Credit]
    outstanding: int
    names: dict[str, str]
    anonymous: set[str]


def display_name(player_id: str, names: dict[str, str], anonymous: set[str]) -> str:
    """A player's public name: never an id, and Anonymous if they asked."""
    return ANONYMOUS if player_id in anonymous else names.get(player_id, UNNAMED)


def slugify(name: str) -> str:
    """A URL-safe folder name for the event's page."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "donation-day"


def get_event(guild_id: str | int) -> Event | None:
    """The guild's donation day settings, if one was ever set up."""
    raw: Any = get_config(guild_id).get("donation_day")
    if not isinstance(raw, dict):
        return None
    return Event(name=str(raw["name"]), goal_usd=int(raw["goal_usd"]),
                 starts_at=str(raw["starts_at"]), page_key=str(raw["page_key"]),
                 active=bool(raw.get("active", False)))


def save_event(guild_id: str | int, event: Event) -> None:
    update_setting(guild_id, "donation_day", dict(event))


def donation_day_running(event: "Event | Mapping[str, Any] | None", now: datetime) -> bool:
    """Whether the server's donation day is on right now: set up, not ended, and past
    its start. A day set up ahead of time waits for its start (server time, as
    /donation_day setup takes it)."""
    if not event or not event.get("active"):
        return False
    try:
        return datetime.fromisoformat(str(event["starts_at"])) <= now
    except (KeyError, TypeError, ValueError):
        # Only a hand-edited config can get here. A staked draft is the safe answer:
        # a donation draft opened on bad config would send the losers' stakes to DraftBot.
        logger.error(f"donation day: unreadable start {event.get('starts_at')!r}; "
                     f"treating the day as not running")
        return False


def draft_kind(*, money_server: bool, event_active: bool) -> str | None:
    """Which kind of draft /draft opens: "donation" while the server's donation day
    runs, "staked" on a money server otherwise, None on a free server.

    The donation day itself is the switch (/donation_day setup and end), so /draft
    carries no option of its own: one would sit in every player's command picker
    on every day that is not a donation day."""
    if not money_server:
        return None
    return "donation" if event_active else "staked"


def _row_key(row: dict[str, Any]) -> tuple[int, str]:
    return (-row["total"], row["name"])


def _credit_time(credit: Credit) -> datetime:
    return credit.at


def tally(credits: list[Credit], outstanding: int, names: dict[str, str],
          anonymous: set[str], event: Event, now: datetime) -> dict[str, Any]:
    """The page payload. Pure, so the arithmetic is testable without a database."""
    won: dict[str, int] = {}
    paid: dict[str, int] = {}
    for credit in credits:
        bucket = won if credit.kind == "won" else paid
        bucket[credit.player_id] = bucket.get(credit.player_id, 0) + credit.amount

    def name(player_id: str) -> str:
        return display_name(player_id, names, anonymous)

    rows = [{"name": name(p), "won": won.get(p, 0), "paid": paid.get(p, 0),
             "total": won.get(p, 0) + paid.get(p, 0)}
            for p in set(won) | set(paid)]
    rows.sort(key=_row_key)
    recent = [{"name": name(c.player_id), "amount": c.amount, "kind": c.kind,
               "at": c.at.isoformat(timespec="minutes")}
              for c in sorted(credits, key=_credit_time, reverse=True)[:RECENT_LIMIT]]
    raised = sum(paid.values())
    return {"name": event["name"], "goal": event["goal_usd"], "raised": raised,
            "pledged": raised + outstanding, "rows": rows, "recent": recent,
            "updated_at": now.isoformat(timespec="minutes")}


async def gather_credits(session: AsyncSession, guild_id: str, bot_id: str,
                         since: datetime) -> tuple[list[Credit], int]:
    """Every credit since `since`, and what losers still owe DraftBot in total."""
    credits: list[Credit] = []

    # WON: a booked donation debt names its draft and one loser; that loser's
    # opponents are the winners, credited their recorded shares.
    booked: dict[str, tuple[str, datetime]] = {}
    for source_id, loser, at in (await session.execute(
            select(DebtLedger.source_id, DebtLedger.player_id, DebtLedger.created_at)
            .where(DebtLedger.guild_id == guild_id,
                   DebtLedger.source_type == DONATION_SOURCE,
                   DebtLedger.counterparty_id == bot_id,
                   DebtLedger.amount < 0, DebtLedger.created_at >= since,
                   TIX_ONLY))).all():
        booked.setdefault(str(source_id).split(":")[1], (loser, at))
    for session_id, (loser, at) in booked.items():
        draft = (await session.execute(
            select(DraftSession.team_a, DraftSession.team_b, DraftSession.friendly_id)
            .where(DraftSession.session_id == session_id))).first()
        if draft is None:
            continue
        team_a, team_b = list(draft.team_a or []), list(draft.team_b or [])
        winners = team_a if loser in team_b else team_b
        shares: dict[str, int] = {
            str(player): int(share or 0) for player, share in (await session.execute(
                select(StakeInfo.player_id, StakeInfo.assigned_stake)
                .where(StakeInfo.session_id == session_id))).all()}
        label = str(draft.friendly_id or session_id)
        credits += [Credit(w, int(shares[w]), at, "won", key=f"won:{session_id}:{w}", draft=label)
                    for w in winners if shares.get(w)]

    # PAID: settlements of debts to DraftBot -- auto-drawn, paid by /wallet pay,
    # or self-marked cash.
    for row_id, player_id, amount, at, settlement_method in (await session.execute(
            select(DebtLedger.id, DebtLedger.player_id, DebtLedger.amount,
                   DebtLedger.created_at, DebtLedger.settlement_method)
            .where(DebtLedger.guild_id == guild_id, DebtLedger.counterparty_id == bot_id,
                   DebtLedger.source_type == "settlement", DebtLedger.amount > 0,
                   DebtLedger.created_at >= since, TIX_ONLY))).all():
        credits.append(Credit(player_id, int(amount), at, "paid", key=f"settle:{row_id}",
                              method=str(settlement_method or "external")))

    # PAID: direct gifts. A debt:<id> transfer is the wallet half of a settlement
    # already counted above, so it is skipped rather than counted twice.
    for tx_id, payer, amount, at, source in (await session.execute(
            select(WalletTx.id, WalletTx.counterparty_id, WalletTx.amount, WalletTx.created_at,
                   WalletTx.source)
            .where(WalletTx.guild_id == guild_id, WalletTx.player_id == bot_id,
                   WalletTx.asset == TIX, WalletTx.kind == "receive",
                   WalletTx.created_at >= since))).all():
        if (source or "").startswith("debt:") or not payer or is_system_account(payer):
            continue
        credits.append(Credit(payer, int(amount), at, "paid", key=f"gift:{tx_id}",
                              method="gift"))

    balances = (await session.execute(
        select(func.sum(DebtLedger.amount))
        .where(DebtLedger.guild_id == guild_id, DebtLedger.counterparty_id == bot_id,
               TIX_ONLY)
        .group_by(DebtLedger.player_id))).scalars().all()
    outstanding = sum(-int(b) for b in balances if b is not None and b < 0)
    return credits, outstanding


async def gather_snapshot(guild_id: str, event: Event, bot_id: str) -> Snapshot:
    """Everything the page and the Discord posts are built from, read once."""
    since = datetime.fromisoformat(event["starts_at"])
    async with db_session() as session:
        credits, outstanding = await gather_credits(session, guild_id, bot_id, since)
        ids = sorted({c.player_id for c in credits})
        names = await resolve_display_names(session, guild_id, ids)
    anonymous = await get_anonymous_donors(ids, guild_id)
    return Snapshot(credits, outstanding, names, anonymous)


async def build_payload(guild_id: str, event: Event, bot_id: str,
                        now: datetime | None = None) -> dict[str, Any]:
    """The whole page payload for one guild's event."""
    s = await gather_snapshot(guild_id, event, bot_id)
    return tally(s.credits, s.outstanding, s.names, s.anonymous, event, now or datetime.now())
