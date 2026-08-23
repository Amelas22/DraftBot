"""Shared draft-log helpers that are new to the reconciliation design:
pool rendering and per-team pool posting. Capture/publish mechanics remain on
DraftSetupManager (the single home); this module holds only the genuinely-new
logic so it can be unit-tested in isolation."""
from __future__ import annotations

import io
from collections import Counter, namedtuple
from datetime import datetime
from functools import partial
from typing import Awaitable, Callable, Iterable

import discord
from loguru import logger
from sqlalchemy import select

from database.db_session import db_session
from helpers.pile_compositor import PileImageBuilder
from helpers.substitutes import TEAM_A_CHANNEL_PREFIX, TEAM_B_CHANNEL_PREFIX
from helpers.utils import DISCORD_THREAD_NAME_LIMIT, THREAD_ARCHIVE_MAX_MINUTES, mention_all
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
    colliding names get the suffix, so the common case stays exactly
    'Alice.txt' / 'Bob.txt'.

    The suffix is the WHOLE Discord id, not a short fragment of it: a
    fragment can collide too (two players whose names sanitise alike and
    whose ids share those digits), which would silently reintroduce the exact
    bug this function exists to prevent. Ids are unique by construction, so
    the full one makes the filename unique by construction -- an ugly
    filename only ever appears in the already-rare collision case."""
    counts = Counter(m.safe for m in members)
    return [
        m._replace(safe=f"{m.safe}-{m.discord_id}") if counts[m.safe] > 1 else m
        for m in members
    ]


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


async def _resolve_destination(bot, destination_id: str | None):
    """The live channel-or-thread named by a stored id, or None if there
    genuinely isn't one (never stored, or Discord confirms it's gone via
    NotFound/Forbidden). Either kind resolves the same way, which is what lets
    one column record whichever of the two carried a team's pools.
    Checked against the cache first, then a real fetch — the cache alone can
    miss a thread across a bot restart.

    Any OTHER `discord.HTTPException` (a transient 5xx, a timeout — exactly
    the kind of blip that made this a retry in the first place) is NOT
    swallowed into "gone": it propagates to the caller, which must abort this
    run rather than read it as "no thread" and open a second one alongside
    the still-live first thread."""
    if not destination_id:
        return None
    cid = int(destination_id)
    destination = bot.get_channel(cid)
    if destination is None:
        try:
            destination = await bot.fetch_channel(cid)
        except (discord.NotFound, discord.Forbidden):
            return None
    return destination


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
    bot_id = getattr(bot.user, "id", None)
    if bot_id is None:
        # Not logged in, so nothing here can be ours to recognise. Bail before
        # the history call rather than scanning and matching nothing.
        return set()
    names: set[str] = set()
    async for message in destination.history(limit=limit):
        if message.author.id != bot_id:
            continue
        names.update(a.filename for a in message.attachments if a.filename.endswith(".txt"))
    return names


async def _post_missing_players(
    destination,
    postable: list[_PostableMember],
    draft_data: dict,
    already_posted: set[str],
) -> tuple[bool, int]:
    """Post each postable member not already in `already_posted` to
    `destination` (a thread, or the channel on the no-thread fallback).
    Guards each send individually — one player's failure is logged and the
    loop continues rather than costing the others their pools.

    Returns `(all_posted, sent)`: whether every postable member ended up
    posted, and how many pools this call actually delivered. The caller needs
    the count to decide whether this destination has become the team's
    delivery record — a fallback that posted nothing has claimed nothing."""
    all_posted = True
    sent = 0
    for member in postable:
        if f"{member.safe}.txt" in already_posted:
            continue
        try:
            await _send_pool(destination, member, draft_data)
            sent += 1
        except Exception as e:
            logger.warning(
                f"[team-logs] failed to post pool for {member.name} ({member.dm_user_id}): {e}"
            )
            all_posted = False
    return all_posted, sent


async def _open_pools_thread(channel, friendly_id: str, player_count: int):
    """The newly-created pools thread for this team, hanging off one summary
    message in the team channel — or None if Discord refused, either the
    summary send or the thread creation itself (typically missing Manage
    Threads). A None sends the caller down the per-player fallback: losing the
    tidy grouping is acceptable, losing people's pools is not."""
    try:
        summary = await channel.send(
            content=f"📥 **Drafted pools** — {friendly_id} ({player_count} players)"
        )
    except discord.HTTPException as e:
        logger.warning(
            f"[team-logs] could not post the pools summary message for {friendly_id}, "
            f"falling back to per-player channel posts: {e}"
        )
        return None

    try:
        return await summary.create_thread(
            name=f"Drafted pools — {friendly_id}"[:DISCORD_THREAD_NAME_LIMIT],
            auto_archive_duration=THREAD_ARCHIVE_MAX_MINUTES,
        )
    except discord.HTTPException as e:
        logger.warning(
            f"[team-logs] thread creation failed for {friendly_id}, "
            f"falling back to per-player channel posts: {e}"
        )
        # Take the summary back down. It now heads an empty thread that will
        # never exist, and leaving it makes the retry non-idempotent: a run
        # whose fallback ALSO fails a player send returns incomplete, so the
        # reconciler re-enters here on its next 60s tick and posts another
        # orphan header — up to 72h of them stacking up in a channel the team
        # is trying to talk in. Best-effort: if the delete is refused too, the
        # players' pools still go out below, which is what matters.
        try:
            await summary.delete()
        except discord.HTTPException as delete_error:
            logger.warning(
                f"[team-logs] could not remove the orphaned pools summary for "
                f"{friendly_id}: {delete_error}"
            )
        return None


async def _tag_team_in_thread(thread, member_discord_ids: list[str], friendly_id: str) -> None:
    """Open the thread by mentioning the team, so it surfaces in their sidebar.

    Discord adds a mentioned member to the thread, and a thread you belong to
    is the one that shows up in the channel list rather than staying buried
    behind the summary message. So the mention is what makes this organised,
    not just polite.

    Mentions the whole team roster, not only the players whose pools are in
    here: a member without a mapped pool is still on the team and still wants
    the thread. Best-effort — the pools are the deliverable, and a failure to
    tag must not cost anyone theirs, nor leave the run looking incomplete.

    Called only on the branch that CREATES the thread, so a retry resuming
    into an existing thread never tags twice.
    """
    mentions = mention_all(member_discord_ids)
    if not mentions:
        return
    try:
        await thread.send(f"{mentions} — your drafted pools for {friendly_id}:")
    except discord.HTTPException as e:
        logger.warning(f"[team-logs] could not tag the team in the pools thread for {friendly_id}: {e}")


async def _post_pools_for_team(
    bot,
    channel: discord.abc.GuildChannel | None,
    destination_id: str | None,
    friendly_id: str,
    member_discord_ids: list[str],
    mapping: dict[str, str],
    draft_data: dict,
    sign_ups: dict,
    persist_destination_id: Callable[[str], Awaitable[None]],
) -> bool:
    """Post every postable team member's pool, into a thread hanging off one
    summary message in the team channel — or, if Discord refuses that thread,
    into the channel itself.

    Whichever of the two ends up carrying the pools becomes this team's
    recorded destination, persisted via `persist_destination_id`. That is the
    load-bearing idea: a retry resumes into the SAME place, so pools are never
    split across a channel and a thread, and a team whose fallback already
    delivered never gets a second, empty thread opened for them later.

    Returns whether every postable player ended up with a pool posted. False
    (a send failure, or an unresolvable stored destination) leaves the
    caller's team_logs_posted_at stamp unset, so the reconciler retries and
    posts only the stragglers.
    """
    postable = _postable_members(member_discord_ids, mapping, draft_data, sign_ups)
    if not postable:
        return True
    if channel is None:
        # The all-or-nothing channel-resolution rule in the caller should
        # prevent this (a team with postable members always has a resolved
        # channel by the time we get here) — guarded defensively rather than
        # raising into the caller.
        return False

    try:
        destination = await _resolve_destination(bot, destination_id)
    except discord.HTTPException as e:
        # A destination is stored but Discord couldn't confirm either way
        # (not a clean "gone" NotFound/Forbidden) -- e.g. a transient 5xx or
        # timeout, exactly the kind of blip a retry exists to ride out.
        # Abort this run rather than risk opening a second thread alongside
        # a first one that may still be perfectly alive.
        logger.warning(
            f"[team-logs] could not resolve the stored pools destination for {friendly_id}, "
            f"aborting this run rather than risk a second thread: {e}"
        )
        return False

    if destination is None:
        # Nothing recorded yet, so this team has never had a pool delivered.
        thread = await _open_pools_thread(channel, friendly_id, len(postable))
        if thread is not None:
            # Persist before posting anyone, so a run that dies partway
            # resumes into this thread instead of opening a second one.
            await persist_destination_id(str(thread.id))
            await _tag_team_in_thread(thread, member_discord_ids, friendly_id)
            destination = thread
        else:
            # Fall back to the channel. Not recorded yet -- only actually
            # landing a pool there claims it (below), so a fallback that
            # delivers nothing leaves the team eligible for a thread next
            # tick rather than being pinned to the channel by a failure.
            destination = channel

    in_channel = destination is channel
    # The channel is general team chat, so its history is bounded; a pools
    # thread holds nothing but the tag and one message per player, so scanning
    # it whole is both cheap and exact.
    already_posted = await _posted_txt_filenames(
        bot, destination, limit=CHANNEL_HISTORY_SCAN_LIMIT if in_channel else None
    )
    all_posted, sent = await _post_missing_players(destination, postable, draft_data, already_posted)

    if in_channel and sent and destination_id is None:
        await persist_destination_id(str(channel.id))

    return all_posted


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
        team_a_destination_id = ds.team_a_pools_destination_id
        team_b_destination_id = ds.team_b_pools_destination_id

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

    async def _persist_destination_id(field: str, destination_id: str) -> None:
        async with db_session() as persist_session:
            row = (await persist_session.execute(
                select(DraftSession).filter(DraftSession.session_id == session_id)
            )).scalar_one_or_none()
            if row is not None:
                setattr(row, field, destination_id)
                await persist_session.commit()

    red_all_posted = await _post_pools_for_team(
        bot, red, team_a_destination_id, friendly_id, team_a, mapping, draft_data, sign_ups,
        persist_destination_id=partial(_persist_destination_id, "team_a_pools_destination_id"),
    )
    blue_all_posted = await _post_pools_for_team(
        bot, blue, team_b_destination_id, friendly_id, team_b, mapping, draft_data, sign_ups,
        persist_destination_id=partial(_persist_destination_id, "team_b_pools_destination_id"),
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
