"""Shared draft-log helpers that are new to the reconciliation design:
pool rendering and per-team pool posting. Capture/publish mechanics remain on
DraftSetupManager (the single home); this module holds only the genuinely-new
logic so it can be unit-tested in isolation."""
from __future__ import annotations

import io
from collections import namedtuple
from datetime import datetime
from typing import Awaitable, Callable, Iterable

import discord
from loguru import logger
from sqlalchemy import select

from database.db_session import db_session
from helpers.pile_compositor import PileImageBuilder
from helpers.substitutes import TEAM_A_CHANNEL_PREFIX, TEAM_B_CHANNEL_PREFIX
from models.draft_session import DraftSession


def _grouped_lines(card_ids: list, carddata: dict) -> list:
    """`"<count> <CardName>"` lines for `card_ids`, grouped by name (using the
    front-face card name) and ordered by first appearance."""
    counts: dict[str, int] = {}
    order: list[str] = []
    for cid in card_ids:
        name = (carddata.get(cid) or {}).get("name")
        if not name:
            continue
        if name not in counts:
            order.append(name)
        counts[name] = counts.get(name, 0) + 1
    return [f"{counts[name]} {name}" for name in order]


def render_pool(draft_data: dict, user_id: str) -> str:
    """Importable decklist for one drafter's full pool: `"<count> <CardName>"`
    lines from `users[user_id].cards`, using the front-face card name. Returns
    "" if the user or their cards are missing."""
    users = draft_data.get("users") or {}
    user = users.get(user_id) or {}
    carddata = draft_data.get("carddata") or {}
    card_ids = user.get("cards") or []
    return "\n".join(_grouped_lines(card_ids, carddata))


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


# One postable team member: someone with a Draftmancer user id and a
# non-empty pool. Members without a mapping or with an empty pool never had a
# post to begin with, so they don't count toward "all posted".
_PostableMember = namedtuple("_PostableMember", "discord_id dm_user_id name safe pool")

THREAD_NAME_LIMIT = 100
TEAM_POOLS_AUTO_ARCHIVE_MINUTES = 10080  # 7 days, the max — matches the tournament match rooms
# Bound on how far back the no-thread fallback scans the team CHANNEL's
# history for already-posted players. The channel is a general team chat, not
# a pools-only thread, so an unbounded scan there could walk a very long,
# mostly-irrelevant history on every retry tick.
CHANNEL_HISTORY_SCAN_LIMIT = 200


def _postable_members(
    member_discord_ids: list[str],
    mapping: dict[str, str],
    draft_data: dict,
    sign_ups: dict,
) -> list[_PostableMember]:
    members: list[_PostableMember] = []
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
        members.append(_PostableMember(discord_id, dm_user_id, name, safe, pool))
    return _dedupe_safe_names(members)


def _dedupe_safe_names(members: list[_PostableMember]) -> list[_PostableMember]:
    """Disambiguate `safe` only where it collides within the team. Sanitising
    strips everything but alphanumerics/space/`_`/`-`, so e.g. 'j.doe' and
    'jdoe', or 'Bob!' and 'Bob?', collapse to the same filename. Left alone,
    the posted-set (computed once, up front, from `{safe}.txt` filenames)
    would make a retry after a partial failure see the one shared `.txt` and
    wrongly treat the OTHER colliding player as already posted too -- lost
    silently, the same failure mode as an unscoped posted-check. Only the
    colliding names get a short suffix (last 4 of their Discord id), so the
    common case stays exactly 'Alice.txt' / 'Bob.txt'."""
    counts: dict[str, int] = {}
    for m in members:
        counts[m.safe] = counts.get(m.safe, 0) + 1
    result: list[_PostableMember] = []
    for m in members:
        if counts[m.safe] > 1:
            m = m._replace(safe=f"{m.safe}-{str(m.discord_id)[-4:]}")
        result.append(m)
    return result


async def _send_pool(destination, member: _PostableMember, draft_data: dict) -> None:
    """Send one player's pool (.txt, plus a best-effort .jpg pile image) to
    `destination` (a channel or thread), with exactly the content and
    attachments the channel posts carried before threading."""
    files = [discord.File(io.BytesIO(member.pool.encode("utf-8")), filename=f"{member.safe}.txt")]

    # Best-effort mana-value pile image (main deck + sideboard) alongside the
    # .txt. The .txt is the deliverable and post_team_logs is reconciler-driven,
    # so any image failure (Scryfall exhaustion -> build None, or an exception)
    # is logged and skipped — never blocking the post.
    try:
        split = split_decklist(draft_data, member.dm_user_id)
        image = await PileImageBuilder().build(
            split["main"], split["side"], draft_data.get("carddata", {})
        )
        if image:
            files.append(discord.File(io.BytesIO(image.getvalue()), filename=f"{member.safe}.jpg"))
    except Exception as e:
        logger.warning(f"[team-logs] deck image failed for {member.name} ({member.dm_user_id}): {e}")

    await destination.send(
        content=f"**{member.name}** — drafted pool ({member.pool.count(chr(10)) + 1} cards):",
        files=files,
    )


async def _resolve_thread(bot, thread_id: str | None):
    """The live thread named by a stored id, or None if there genuinely isn't
    one (never stored, or Discord confirms it's gone via NotFound/Forbidden).
    Checked against the cache first, then a real fetch — the cache alone can
    miss a thread across a bot restart.

    Any OTHER `discord.HTTPException` (a transient 5xx, a timeout — exactly
    the kind of blip that made this a retry in the first place) is NOT
    swallowed into "gone": it propagates to the caller, which must abort this
    run rather than read it as "no thread" and open a second one alongside
    the still-live first thread."""
    if not thread_id:
        return None
    tid = int(thread_id)
    thread = bot.get_channel(tid)
    if thread is None:
        try:
            thread = await bot.fetch_channel(tid)
        except (discord.NotFound, discord.Forbidden):
            return None
    return thread


async def _posted_txt_filenames(bot, destination, limit: int | None = None) -> set[str]:
    """Filenames of every `.txt` attachment the BOT has already posted in
    `destination` (the pools thread, or — in the no-thread fallback — the
    team channel itself), used to work out who is already posted on a retry.

    Scoped to messages authored by the bot: `destination` is a channel/thread
    the team actually talks in, so a player uploading their own `Alice.txt`
    there must never be mistaken for a completed post — that would silently
    skip the real Alice pool and stamp the session complete.

    Matched on the attachment filename rather than the message text: the
    `.txt` is the deliverable and its name is derived per player, whereas the
    message wording is the kind of thing that gets reworded later — which
    would silently make every player look unposted and re-post the lot."""
    names: set[str] = set()
    bot_id = getattr(getattr(bot, "user", None), "id", None)
    async for message in destination.history(limit=limit):
        author = getattr(message, "author", None)
        author_id = getattr(author, "id", None)
        if bot_id is None or author_id != bot_id:
            continue
        for attachment in message.attachments:
            if attachment.filename.endswith(".txt"):
                names.add(attachment.filename)
    return names


async def _post_missing_players(
    destination,
    postable: list[_PostableMember],
    draft_data: dict,
    already_posted: set[str],
) -> bool:
    """Post each postable member not already in `already_posted` to
    `destination` (a thread, or the channel on the no-thread fallback).
    Guards each send individually — one player's failure is logged and the
    loop continues rather than costing the others their pools. Returns True
    iff every postable member ends up posted."""
    all_posted = True
    for member in postable:
        if f"{member.safe}.txt" in already_posted:
            continue
        try:
            await _send_pool(destination, member, draft_data)
        except Exception as e:
            logger.warning(
                f"[team-logs] failed to post pool for {member.name} ({member.dm_user_id}): {e}"
            )
            all_posted = False
    return all_posted


async def _post_pools_for_team(
    bot,
    channel: discord.abc.GuildChannel | None,
    thread_id: str | None,
    friendly_id: str,
    member_discord_ids: list[str],
    mapping: dict[str, str],
    draft_data: dict,
    sign_ups: dict,
    persist_thread_id: Callable[[str], Awaitable[None]],
) -> tuple[str | None, bool]:
    """Post every postable team member's pool into a thread hanging off one
    summary message in the team channel, resuming into the thread named by
    `thread_id` rather than re-posting or opening a second one.

    Returns `(thread_id, all_posted)`: `thread_id` is what the caller should
    keep on the session (unchanged, newly created, or still None if Discord
    refused thread creation and we fell back to channel posts); `all_posted`
    is False if any postable player is still missing a pool at the end (a
    send failure, or a still-missing player on a fallback pass).
    """
    postable = _postable_members(member_discord_ids, mapping, draft_data, sign_ups)
    if not postable:
        return thread_id, True
    if channel is None:
        # The all-or-nothing channel-resolution rule in the caller should
        # prevent this (a team with postable members always has a resolved
        # channel by the time we get here) — guarded defensively rather than
        # raising into the caller.
        return thread_id, False

    try:
        thread = await _resolve_thread(bot, thread_id)
    except discord.HTTPException as e:
        # A stored thread id exists but Discord couldn't confirm either way
        # (not a clean "gone" NotFound/Forbidden) -- e.g. a transient 5xx or
        # timeout, exactly the kind of blip a retry exists to ride out.
        # Abort this run rather than risk opening a second thread alongside
        # a first one that may still be perfectly alive.
        logger.warning(
            f"[team-logs] could not resolve the stored pools thread for {friendly_id}, "
            f"aborting this run rather than risk a second thread: {e}"
        )
        return thread_id, False

    if thread is None:
        try:
            summary = await channel.send(
                content=f"📥 **Drafted pools** — {friendly_id} ({len(postable)} players)"
            )
        except discord.HTTPException as e:
            logger.warning(
                f"[team-logs] could not post the pools summary message for {friendly_id}, "
                f"falling back to per-player channel posts: {e}"
            )
            already_posted = await _posted_txt_filenames(bot, channel, limit=CHANNEL_HISTORY_SCAN_LIMIT)
            all_posted = await _post_missing_players(channel, postable, draft_data, already_posted)
            return thread_id, all_posted

        try:
            thread = await summary.create_thread(
                name=f"Drafted pools — {friendly_id}"[:THREAD_NAME_LIMIT],
                auto_archive_duration=TEAM_POOLS_AUTO_ARCHIVE_MINUTES,
            )
        except discord.HTTPException as e:
            logger.warning(
                f"[team-logs] thread creation failed for {friendly_id}, "
                f"falling back to per-player channel posts: {e}"
            )
            already_posted = await _posted_txt_filenames(bot, channel, limit=CHANNEL_HISTORY_SCAN_LIMIT)
            all_posted = await _post_missing_players(channel, postable, draft_data, already_posted)
            return thread_id, all_posted

        # The thread exists from here on out — persist it before posting any
        # player, so a run that dies partway through resumes into this thread
        # on the next tick instead of opening a second one.
        thread_id = str(thread.id)
        await persist_thread_id(thread_id)

    already_posted = await _posted_txt_filenames(bot, thread)
    all_posted = await _post_missing_players(thread, postable, draft_data, already_posted)
    return thread_id, all_posted


# Sessions with a post_team_logs run currently in flight. The endDraft push
# path and the 60s reconciler tick can both call within one run's duration
# (image builds can stretch a run past several ticks), and the
# team_logs_posted_at stamp is only written at the END of a successful run —
# so the stamp alone cannot prevent overlapping duplicate runs.
_POSTS_IN_FLIGHT: set[str] = set()


async def post_team_logs(session_id: str, bot) -> bool:
    """Post each team's own members' pools to its private team channel, then
    stamp team_logs_posted_at. Idempotent; safe to call before unlock_at.
    Concurrent calls for the same session are dropped (False) while one runs."""
    if session_id in _POSTS_IN_FLIGHT:
        logger.info(f"post_team_logs already in flight for {session_id}; dropping duplicate call")
        return False
    _POSTS_IN_FLIGHT.add(session_id)
    try:
        return await _post_team_logs_locked(session_id, bot)
    finally:
        _POSTS_IN_FLIGHT.discard(session_id)


async def _post_team_logs_locked(session_id: str, bot) -> bool:
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
        friendly_id = ds.friendly_id or ds.draft_id or session_id
        team_a_thread_id = ds.team_a_pools_thread_id
        team_b_thread_id = ds.team_b_pools_thread_id

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
    # needed team's channel didn't resolve, this run is incomplete. Post
    # nothing (all-or-nothing): posting best-effort to whichever channel(s)
    # did resolve would re-post to that already-served, player-visible
    # channel on every retry tick until the other channel resolves too.
    red_ok = red is not None or not team_a
    blue_ok = blue is not None or not team_b
    fully_resolved = red_ok and blue_ok

    if not fully_resolved:
        logger.warning(
            f"post_team_logs: only partial team channels resolved for session {session_id} "
            f"(red={'ok' if red_ok else 'missing'}, blue={'ok' if blue_ok else 'missing'}); "
            "posting nothing this call and leaving team_logs_posted_at unset for retry"
        )
        return False

    async def _persist_thread_id(field: str, thread_id: str) -> None:
        async with db_session() as persist_session:
            row = (await persist_session.execute(
                select(DraftSession).filter(DraftSession.session_id == session_id)
            )).scalar_one_or_none()
            if row is not None:
                setattr(row, field, thread_id)
                await persist_session.commit()

    _, red_all_posted = await _post_pools_for_team(
        bot, red, team_a_thread_id, friendly_id, team_a, mapping, draft_data, sign_ups,
        persist_thread_id=lambda tid: _persist_thread_id("team_a_pools_thread_id", tid),
    )
    _, blue_all_posted = await _post_pools_for_team(
        bot, blue, team_b_thread_id, friendly_id, team_b, mapping, draft_data, sign_ups,
        persist_thread_id=lambda tid: _persist_thread_id("team_b_pools_thread_id", tid),
    )

    if not (red_all_posted and blue_all_posted):
        # Some player(s) are still missing a pool (a send failure, or a
        # thread-creation refusal whose fallback pass also failed). Leave
        # team_logs_posted_at unset so the reconciler retries and posts only
        # the stragglers next tick.
        logger.warning(
            f"post_team_logs: not every player posted for session {session_id} "
            f"(red={'ok' if red_all_posted else 'incomplete'}, "
            f"blue={'ok' if blue_all_posted else 'incomplete'}); "
            "leaving team_logs_posted_at unset for retry"
        )
        return False

    async with db_session() as session:
        ds = (await session.execute(
            select(DraftSession).filter(DraftSession.session_id == session_id)
        )).scalar_one_or_none()
        if ds is not None:
            ds.team_logs_posted_at = datetime.now()
            await session.commit()
    return True


_BASIC_LAND_NAMES = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest", "C": "Wastes"}


def split_decklist(draft_data: dict, user_id: str) -> dict:
    """Split a drafter's pool into built main deck, basic-land counts, and sideboard.

    Uses users[user_id].decklist when the player built a deck (non-empty main);
    otherwise falls back to the full pool as main with no basics/sideboard.
    """
    user = (draft_data.get("users") or {}).get(user_id) or {}
    decklist = user.get("decklist") or {}
    main = decklist.get("main") or []
    if main:
        return {
            "main": list(main),
            "basics": dict(decklist.get("lands") or {}),
            "side": list(decklist.get("side") or []),
        }
    return {"main": list(user.get("cards") or []), "basics": {}, "side": []}


def build_mtgo_deck_text(split: dict, carddata: dict) -> str:
    """MTGO-format deck text: main + basics, then a blank line and the sideboard
    (sideboard block omitted when empty)."""
    main_lines = _grouped_lines(split.get("main") or [], carddata)
    basics = split.get("basics") or {}
    # Emit basics in a stable WUBRGC order (not the source-map order).
    for color, name in _BASIC_LAND_NAMES.items():
        count = basics.get(color, 0)
        if count:
            main_lines.append(f"{count} {name}")
    side_lines = _grouped_lines(split.get("side") or [], carddata)
    if side_lines:
        return "\n".join(main_lines) + "\n\n" + "\n".join(side_lines)
    return "\n".join(main_lines)
