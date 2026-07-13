"""Shared draft-log helpers that are new to the reconciliation design:
pool rendering and per-team pool posting. Capture/publish mechanics remain on
DraftSetupManager (the single home); this module holds only the genuinely-new
logic so it can be unit-tested in isolation."""
from __future__ import annotations

import io
from datetime import datetime
from typing import Iterable

import discord
from loguru import logger
from sqlalchemy import select

from database.db_session import db_session
from helpers.substitutes import TEAM_A_CHANNEL_PREFIX, TEAM_B_CHANNEL_PREFIX
from models.draft_session import DraftSession


def render_pool(draft_data: dict, user_id: str) -> str:
    """Importable decklist for one drafter's full pool: `"<count> <CardName>"`
    lines from `users[user_id].cards`, using the front-face card name. Returns
    "" if the user or their cards are missing."""
    users = draft_data.get("users") or {}
    user = users.get(user_id) or {}
    carddata = draft_data.get("carddata") or {}
    card_ids = user.get("cards") or []

    counts: dict[str, int] = {}
    order: list[str] = []
    for cid in card_ids:
        name = (carddata.get(cid) or {}).get("name")
        if not name:
            continue
        if name not in counts:
            order.append(name)
        counts[name] = counts.get(name, 0) + 1
    return "\n".join(f"{counts[name]} {name}" for name in order)


def map_discord_to_draftmancer(draft_data: dict, sign_ups: dict) -> dict[str, str]:
    """Map Discord user ids -> Draftmancer user ids by seat order, mirroring the
    mapping capture_draft_log uses for pack_first_picks (sign-up insertion order
    lined up against users sorted by seatNum).

    Bot users (isBot truthy) are excluded before alignment, since they never
    correspond to a Discord sign-up and would otherwise shift the mapping. If
    the remaining player count doesn't match the sign-up count, we can't trust
    a positional alignment - return {} rather than risk posting one player's
    pool to a different player's private channel."""
    discord_ids = list((sign_ups or {}).keys())
    real_users = [
        item for item in (draft_data.get("users") or {}).items()
        if not item[1].get("isBot")
    ]
    if len(real_users) != len(discord_ids):
        logger.warning(
            "map_discord_to_draftmancer: player count mismatch "
            f"({len(real_users)} draftmancer players vs {len(discord_ids)} sign-ups); "
            "refusing to guess an alignment, returning empty mapping"
        )
        return {}
    sorted_users = sorted(real_users, key=lambda item: item[1].get("seatNum", 999))
    mapping: dict[str, str] = {}
    for idx, (dm_user_id, _) in enumerate(sorted_users):
        mapping[discord_ids[idx]] = dm_user_id
    return mapping


def _find_team_channel(
    guild: discord.Guild,
    channel_ids: Iterable[int | str],
    prefix: str,
) -> discord.abc.GuildChannel | None:
    """Resolve the private team channel whose name starts with `prefix` (e.g.
    'Red-Team') among the session's channel_ids."""
    prefix_lower = prefix.lower()
    for cid in channel_ids or []:
        channel = guild.get_channel(int(cid))
        if channel is not None and getattr(channel, "name", "").lower().startswith(prefix_lower):
            return channel
    return None


async def _post_pools_for_team(
    channel: discord.abc.GuildChannel | None,
    member_discord_ids: list[str],
    mapping: dict[str, str],
    draft_data: dict,
    sign_ups: dict,
) -> None:
    """Post one .txt pool attachment per team member to their team channel."""
    if channel is None:
        return
    for discord_id in member_discord_ids or []:
        dm_user_id = mapping.get(discord_id)
        if not dm_user_id:
            continue
        pool = render_pool(draft_data, dm_user_id)
        if not pool:
            continue
        name = (draft_data["users"][dm_user_id].get("userName")
                or (sign_ups or {}).get(discord_id) or discord_id)
        safe = "".join(c for c in str(name) if c.isalnum() or c in " _-").strip() or str(discord_id)
        fp = io.BytesIO(pool.encode("utf-8"))
        await channel.send(
            content=f"**{name}** — drafted pool ({pool.count(chr(10)) + 1} cards):",
            file=discord.File(fp, filename=f"{safe}.txt"),
        )


async def post_team_logs(session_id: str, bot) -> bool:
    """Post each team's own members' pools to its private team channel, then
    stamp team_logs_posted_at. Idempotent; safe to call before unlock_at."""
    async with db_session() as session:
        ds = (await session.execute(
            select(DraftSession).filter(DraftSession.session_id == session_id)
        )).scalar_one_or_none()
        if ds is None:
            return False
        if ds.team_logs_posted_at is not None:
            return True
        draft_data = ds.draft_data
        if not draft_data:
            return False
        team_a = list(ds.team_a or [])
        team_b = list(ds.team_b or [])
        channel_ids = list(ds.channel_ids or [])
        sign_ups = dict(ds.sign_ups or {})
        guild_id = ds.guild_id

    guild = bot.get_guild(int(guild_id)) if guild_id else None
    if guild is None:
        return False

    mapping = map_discord_to_draftmancer(draft_data, sign_ups)
    red = _find_team_channel(guild, channel_ids, TEAM_A_CHANNEL_PREFIX)
    blue = _find_team_channel(guild, channel_ids, TEAM_B_CHANNEL_PREFIX)

    if red is None and blue is None:
        # Neither team channel resolved, so nothing could be posted. Leave
        # team_logs_posted_at unstamped so the reconciler retries later.
        logger.warning(
            f"post_team_logs: no team channels resolved for session {session_id}; "
            "leaving team_logs_posted_at unset for retry"
        )
        return False

    # A team only "needs" a channel if it has members to post pools for. If a
    # needed team's channel didn't resolve, this run is incomplete: still post
    # best-effort to whichever channel(s) did resolve, but don't stamp, so the
    # reconciler retries and the unresolved team isn't permanently skipped.
    red_ok = red is not None or not team_a
    blue_ok = blue is not None or not team_b
    fully_resolved = red_ok and blue_ok

    if not fully_resolved:
        logger.warning(
            f"post_team_logs: only partial team channels resolved for session {session_id} "
            f"(red={'ok' if red_ok else 'missing'}, blue={'ok' if blue_ok else 'missing'}); "
            "posting best-effort and leaving team_logs_posted_at unset for retry"
        )

    await _post_pools_for_team(red, team_a, mapping, draft_data, sign_ups)
    await _post_pools_for_team(blue, team_b, mapping, draft_data, sign_ups)

    if not fully_resolved:
        return False

    async with db_session() as session:
        ds = (await session.execute(
            select(DraftSession).filter(DraftSession.session_id == session_id)
        )).scalar_one_or_none()
        if ds is not None:
            ds.team_logs_posted_at = datetime.now()
            await session.commit()
    return True
