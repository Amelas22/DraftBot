"""A player's own settings in one server: loading them and flipping one.

No Discord UI here (player_settings_view draws them). Every value is read and
written through its existing preference_service getter and setter, so /settings
and the other places that change a setting (a draft's sign-up panel, the
library prompt) agree on what is stored.
"""
import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import discord
from loguru import logger
from sqlalchemy import select

from config import is_money_server
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
from services.library_access_service import may_borrow
from services.library_prompt import COULD_NOT_CHECK, library_to_ask_about, reserve_and_say
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


async def load_settings(player_id: str, guild_id: str) -> Settings:
    return Settings(
        dms=bool(await get_player_dm_notification_preference(player_id, guild_id)),
        hide_from_ladder=await get_hide_from_sr_ladder(player_id, guild_id),
        anonymous=await _anonymous(player_id, guild_id),
        stake_cap=(bool(await get_player_bet_capping_preference(player_id, guild_id))
                   if is_money_server(guild_id) else None),
        library=await get_uses_library(player_id, guild_id),
        library_offered=await _library_offered(player_id, guild_id),
    )


async def flip(player_id: str, guild_id: str, setting: Setting) -> bool | None:
    """Write the opposite of what is stored now, and return it.

    It reads the stored value rather than trusting the caller's copy, so a
    click on an older /settings message cannot write back a stale state.
    None if the save failed.
    """
    match setting:
        case Setting.DMS:
            new = not await get_player_dm_notification_preference(player_id, guild_id)
            saved = await update_player_dm_notification_preference(player_id, guild_id, new)
        case Setting.HIDE_FROM_LADDER:
            new = not await get_hide_from_sr_ladder(player_id, guild_id)
            saved = await set_hide_from_sr_ladder(player_id, guild_id, new)
        case Setting.ANONYMOUS:
            new = not await _anonymous(player_id, guild_id)
            saved = await set_donation_anonymous(player_id, guild_id, new)
        case Setting.STAKE_CAP:
            # Only the preference; the caller carries it into live sign-ups
            # (apply_cap_to_live_drafts), which needs the bot.
            new = not await get_player_bet_capping_preference(player_id, guild_id)
            saved = await update_player_bet_capping_preference(player_id, guild_id, new)
        case Setting.LIBRARY:
            # Not set reads as off, so the first click turns it on. The caller
            # holds copies in live sign-ups (apply_library_to_live_drafts).
            new = not await get_uses_library(player_id, guild_id)
            saved = await set_uses_library(player_id, guild_id, new)
    return new if saved else None


# --- carrying the cap into the drafts a player is signed up for ------------

async def live_drafts(player_id: str, guild_id: str) -> list[DraftSession]:
    """The drafts in this server the player is signed up for whose entries can
    still change: no stage yet, teams not made, pool not matched, not expired."""
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(DraftSession).where(
                DraftSession.guild_id == guild_id,
                DraftSession.teams_start_time.is_(None),
                DraftSession.pool_matched_at.is_(None),
                DraftSession.deletion_time > datetime.now(),
                # Change Entry / Settings stops at 'teams'; a draft at any
                # stage is past the point where an entry can change.
                DraftSession.session_stage.is_(None),
            ).order_by(DraftSession.id))).scalars().all()
    # sign_ups is a JSON column, so membership is checked here, not in SQL.
    return [d for d in rows if str(player_id) in {str(k) for k in (d.sign_ups or {})}]


async def apply_cap_to_entry(bot: Any, session_id: str, player_id: str, is_capped: bool,
                             redraw: bool = True) -> bool:
    """Set the cap on the player's entry in one draft and redraw its board.

    False when the player has no entry (StakeInfo) in that draft. With
    redraw=False the caller redraws the board itself, once it has done what
    must not depend on that redraw succeeding.
    """
    async with AsyncSessionLocal() as session:
        async with session.begin():
            stake = (await session.execute(select(StakeInfo).where(
                StakeInfo.session_id == session_id,
                StakeInfo.player_id == player_id,
            ))).scalars().first()
            if stake is None:
                return False
            stake.is_capped = is_capped
    if redraw:
        from views import update_draft_message
        await update_draft_message(bot, session_id)
    return True


@dataclass(frozen=True)
class LiveResult:
    changed: list[DraftSession] = field(default_factory=lambda: list[DraftSession]())
    failed: list[DraftSession] = field(default_factory=lambda: list[DraftSession]())


async def apply_cap_to_live_drafts(bot: Any, player_id: str, guild_id: str,
                                   is_capped: bool) -> LiveResult:
    """Carry a new cap into every live draft the player has an entry in.

    One draft failing does not stop the rest; it is reported in `failed`.
    """
    result = LiveResult()
    for draft in await live_drafts(player_id, guild_id):
        try:
            if await apply_cap_to_entry(bot, draft.session_id, player_id, is_capped):
                result.changed.append(draft)
        except Exception:
            logger.exception("settings: couldn't set the cap for {} in draft {}",
                             player_id, draft.session_id)
            result.failed.append(draft)
    return result


def _link(d: DraftSession) -> str:
    return f"https://discord.com/channels/{d.guild_id}/{d.draft_channel_id}/{d.message_id}"


def _links(drafts: list[DraftSession]) -> str:
    return ", ".join(_link(d) for d in drafts)


def _naming(draft: DraftSession, text: str) -> str:
    """The text, under a link to the draft's board so it says which draft;
    as it is when the board's ids are missing."""
    if not (draft.guild_id and draft.draft_channel_id and draft.message_id):
        return text
    return f"{_link(draft)}\n{text}"


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
                                       ) -> list[tuple[DraftSession, str | None,
                                                       discord.ui.View | None]]:
    """After switching the library on: hold a copy in each live draft whose
    cube the library lends this player, and say where each stands.

    Switching off needs nothing here: a held copy goes once its player no
    longer uses the library. One draft failing does not stop the rest; it is
    reported with COULD_NOT_CHECK. Each text starts with a link to that
    draft's board.
    """
    results: list[tuple[DraftSession, str | None, discord.ui.View | None]] = []
    for draft in await live_drafts(player_id, guild_id):
        try:
            library = await library_to_ask_about(guild_id, draft.cube, player_id)
            if library is None:
                continue
            text, view = await reserve_and_say(player_id, draft, library, reclaim=True)
        except Exception:
            logger.exception("settings: couldn't hold a library copy for {} in draft {}",
                             player_id, draft.session_id)
            text, view = COULD_NOT_CHECK, None
        results.append((draft, None if text is None else _naming(draft, text), view))
    return results
