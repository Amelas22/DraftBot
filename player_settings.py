"""A player's own settings in one server: loading them and flipping one.

No Discord UI here (player_settings_view draws them). Every value is read and
written through its existing preference_service getter and setter, so /settings
and the other places that change a setting (a draft's sign-up panel, the
library prompt) agree on what is stored.
"""
import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import discord
from loguru import logger
from sqlalchemy import exists, select, update

from config import is_money_server, scheduler_enabled
from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from models.stake import StakeInfo
from preference_service import (
    get_anonymous_donors,
    get_hide_from_sr_ladder,
    get_player_bet_capping_preference,
    get_player_dm_notification_preference,
    get_uses_library,
    set_donation_anonymous,
    set_hide_from_sr_ladder,
    set_uses_library,
    update_player_bet_capping_preference,
    update_player_dm_notification_preference,
)
from services.availability import load_usual_week, next_week_lines
from services.library_access_service import may_borrow
from services.library_prompt import COULD_NOT_CHECK, library_to_ask_about, reserve_and_say
from services.library_reservation import queue_link
from services.library_service import rental_library_for


class Setting(enum.Enum):
    STAKE_CAP = "stake_cap"
    LIBRARY = "library"
    DMS = "dms"
    HIDE_FROM_LADDER = "hide_from_ladder"
    ANONYMOUS = "anonymous"


@dataclass(frozen=True)
class Settings:
    dms: bool
    hide_from_ladder: bool
    anonymous: bool
    stake_cap: bool | None = None    # the stored cap on a money server; None elsewhere
    library: bool | None = None      # uses_library as stored; None = not set
    library_offered: bool = False
    scheduler: bool = False
    usual_week: list[str] | None = None   # next-week timestamp lines; None = never saved


async def _anonymous(player_id: str, guild_id: str) -> bool:
    return player_id in await get_anonymous_donors([player_id], guild_id)


async def _library_offered(player_id: str, guild_id: str) -> bool:
    """Whether this server has a rental library this player may borrow from.

    A failed lookup only leaves the library row out; the rest still loads.
    """
    try:
        library = await rental_library_for(guild_id)
        return library is not None and await may_borrow(library.id, player_id)
    except Exception:
        logger.exception("settings: couldn't look up the library for {} in {}",
                         player_id, guild_id)
        return False


async def _usual_week(player_id: str) -> list[str] | None:
    """The saved week as timestamp lines; None if never saved or unreadable.

    A failure only leaves the week out; the rest of /settings still loads.
    """
    try:
        week = await load_usual_week(player_id)
        return None if week is None else next_week_lines(week, datetime.now(timezone.utc))
    except Exception:
        logger.exception("settings: couldn't read the usual week for {}", player_id)
        return None


async def load_settings(player_id: str, guild_id: str) -> Settings:
    scheduler = bool(scheduler_enabled(guild_id))
    return Settings(
        dms=bool(await get_player_dm_notification_preference(player_id, guild_id)),
        hide_from_ladder=await get_hide_from_sr_ladder(player_id, guild_id),
        anonymous=await _anonymous(player_id, guild_id),
        stake_cap=(bool(await get_player_bet_capping_preference(player_id, guild_id))
                   if is_money_server(guild_id) else None),
        library=await get_uses_library(player_id, guild_id),
        library_offered=await _library_offered(player_id, guild_id),
        scheduler=scheduler,
        usual_week=await _usual_week(player_id) if scheduler else None,
    )


_Getter = Callable[[str, str], Awaitable[bool]]
_Setter = Callable[[str, str, bool], Awaitable[Any]]    # truthy when saved


def _as_bool(get: Callable[[str, str], Awaitable[Any]]) -> _Getter:
    async def read(player_id: str, guild_id: str) -> bool:
        return bool(await get(player_id, guild_id))
    return read


# Each setting's stored value, read as a bool, and its setter. The library's
# "not set" reads as off, so the first click turns it on. The stake cap and
# the library only save the preference here; the caller carries them into
# live sign-ups (apply_cap_to_live_drafts, apply_library_to_live_drafts).
_STORE: dict[Setting, tuple[_Getter, _Setter]] = {
    Setting.DMS: (_as_bool(get_player_dm_notification_preference),
                  update_player_dm_notification_preference),
    Setting.HIDE_FROM_LADDER: (_as_bool(get_hide_from_sr_ladder), set_hide_from_sr_ladder),
    Setting.ANONYMOUS: (_anonymous, set_donation_anonymous),
    Setting.STAKE_CAP: (_as_bool(get_player_bet_capping_preference),
                        update_player_bet_capping_preference),
    Setting.LIBRARY: (_as_bool(get_uses_library), set_uses_library),
}


async def flip(player_id: str, guild_id: str, setting: Setting) -> bool | None:
    """Write the opposite of what is stored now, and return it.

    It reads the stored value rather than trusting the caller's copy, so a
    click on an older /settings message cannot write back a stale state.
    None if the save failed.
    """
    get, put = _STORE[setting]
    new = not await get(player_id, guild_id)
    return new if await put(player_id, guild_id, new) else None


# --- carrying the cap into the drafts a player is signed up for ------------

# A draft whose entries can still change. Change Entry / Settings stops at
# 'teams', and matching the pool reads the caps, so a draft at any stage, with
# teams made, or with its pool matched is past that point.
_OPEN = (DraftSession.session_stage.is_(None),
         DraftSession.teams_start_time.is_(None),
         DraftSession.pool_matched_at.is_(None))


def _still_open(draft: DraftSession | None) -> bool:
    return (draft is not None and draft.session_stage is None
            and draft.teams_start_time is None and draft.pool_matched_at is None)


async def live_drafts(player_id: str, guild_id: str) -> list[DraftSession]:
    """The drafts in this server the player is signed up for whose entries can
    still change: no stage yet, teams not made, pool not matched, not expired."""
    # Stricter than library_boards._taking_signups: the panel's controls stop
    # at 'teams', and matching the pool reads the caps.
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(DraftSession).where(
                DraftSession.guild_id == guild_id,
                DraftSession.deletion_time > datetime.now(),
                *_OPEN,
            ).order_by(DraftSession.id))).scalars().all()
    # sign_ups is a JSON column, so membership is checked here, not in SQL.
    return [d for d in rows if str(player_id) in {str(k) for k in (d.sign_ups or {})}]


CapOutcome = Literal["changed", "no_entry", "closed"]


async def apply_cap_to_entry(session_id: str, player_id: str, is_capped: bool) -> CapOutcome:
    """Set the cap on the player's entry in one draft, only while it is open.

    The check and the write are one statement, so a draft that moves on in
    the meantime (team creation commits its stage before the pool is matched)
    keeps the entry it had. "no_entry" when the player has no StakeInfo
    there; "closed" when they do but the draft is past changing it. The
    caller redraws the board.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            entry = (StakeInfo.session_id == session_id, StakeInfo.player_id == player_id)
            written = await session.execute(
                update(StakeInfo)
                .where(*entry, exists().where(DraftSession.session_id == session_id, *_OPEN))
                .values(is_capped=is_capped))
            if written.rowcount:
                return "changed"
            found = (await session.execute(select(StakeInfo.id).where(*entry))).first()
    return "closed" if found is not None else "no_entry"


@dataclass
class LiveResult:
    changed: list[DraftSession] = field(default_factory=list)
    failed: list[DraftSession] = field(default_factory=list)


async def apply_cap_to_live_drafts(bot: Any, player_id: str, guild_id: str,
                                   is_capped: bool) -> LiveResult:
    """Carry a new cap into every live draft the player has an entry in.

    One draft failing does not stop the rest; it is reported in `failed`. An
    entry counts as changed once its write commits: a board that then fails
    to redraw is only logged, since the entry itself is right.
    """
    from views import update_draft_message
    result = LiveResult()
    for draft in await live_drafts(player_id, guild_id):
        try:
            outcome = await apply_cap_to_entry(draft.session_id, player_id, is_capped)
        except Exception:
            logger.exception("settings: couldn't set the cap for {} in draft {}",
                             player_id, draft.session_id)
            result.failed.append(draft)
            continue
        if outcome != "changed":
            continue    # no entry there, or the draft moved on meanwhile
        result.changed.append(draft)
        try:
            await update_draft_message(bot, draft.session_id)
        except Exception:
            logger.exception("settings: set the cap for {} in draft {} but couldn't "
                             "redraw its board", player_id, draft.session_id)
    return result


def _links(drafts: list[DraftSession]) -> str:
    return ", ".join(link for d in drafts if (link := queue_link(d)) is not None)


def _naming(draft: DraftSession, text: str) -> str:
    """The text, under a link to the draft's board so it says which draft;
    as it is when the board's ids are missing."""
    link = queue_link(draft)
    return text if link is None else f"{link}\n{text}"


def live_result_text(result: LiveResult) -> str:
    """What to tell the player about their live sign-ups; "" when nothing to say."""
    parts: list[str] = []
    if result.changed:
        parts.append(f"Also changed in your sign-up for these drafts: {_links(result.changed)}")
    if result.failed:
        parts.append(f"⚠️ Couldn't update {_links(result.failed)}; "
                     "use that draft's **Change Entry / Settings** button.")
    return "\n".join(parts)


# --- carrying the card library into the drafts a player is signed up for ---

async def apply_library_to_live_drafts(player_id: str, guild_id: str
                                       ) -> list[tuple[str, discord.ui.View | None]]:
    """After switching the library on: hold a copy in each live draft whose
    cube the library lends this player, and say where each stands.

    Switching off needs nothing here: a held copy goes once its player no
    longer uses the library. One draft failing does not stop the rest; it is
    reported with COULD_NOT_CHECK. Each text starts with a link to that
    draft's board; a draft with nothing to say is left out.
    """
    results: list[tuple[str, discord.ui.View | None]] = []
    for draft in await live_drafts(player_id, guild_id):
        try:
            library = await library_to_ask_about(guild_id, draft.cube, player_id)
            if library is None:
                continue
            # Reclaiming takes the copy back from another queue, so not for a
            # draft that started while the library was being checked.
            fresh = await DraftSession.get_by_session_id(draft.session_id)
            if not _still_open(fresh):
                continue
            text, view = await reserve_and_say(player_id, fresh, library, reclaim=True)
        except Exception:
            logger.exception("settings: couldn't hold a library copy for {} in draft {}",
                             player_id, draft.session_id)
            text, view = COULD_NOT_CHECK, None
        if text is not None:
            results.append((_naming(draft, text), view))
    return results
