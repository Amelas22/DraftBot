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
from helpers.utils import (
    DISCORD_THREAD_NAME_LIMIT, THREAD_ARCHIVE_MAX_MINUTES, mention_all, send_then_mention,
)
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

# The outcome of trying to open a pools thread. `permanent` separates "Discord
# will never allow this" (no Manage Threads, channel gone) from "not right now"
# (a 5xx, a timeout, a TLS error mid-upload). The private path ignores it -- it
# falls back to the channel either way -- but the open path has no fallback, so
# the distinction is the difference between dropping the thread on purpose and
# losing it forever to a blip.
_ThreadAttempt = namedtuple("_ThreadAttempt", "thread permanent")

# Bound on how far back the team CHANNEL's history is scanned for
# already-posted players. Applies whenever the channel is the destination --
# the run that fell back, and every retry after it, since the channel is then
# the recorded destination. The channel is general team chat, not a pools-only
# thread, so an unbounded scan there could walk a very long, mostly-irrelevant
# history on every tick.
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
    filename only ever appears in the already-rare collision case.

    Checked against every name already handed out, not just the ones that
    collided on the way in. A display name is long enough to hold 'Bob-' plus
    a real snowflake, so a player called literally 'Bob-<someone's id>' can
    collide with the suffix GENERATED for that someone -- and since the
    posted-check reads filenames, that suppresses the victim's pool on every
    retry, for good. Rare by accident, trivial to do on purpose."""
    counts = Counter(m.safe for m in members)
    used: set[str] = set()
    out = []
    for m in members:
        name = m.safe if counts[m.safe] == 1 else f"{m.safe}-{m.discord_id}"
        while name in used:
            # Strictly lengthens, so this terminates; reached only when a name
            # was crafted to look like another player's generated one.
            name = f"{name}-{m.discord_id}"
        used.add(name)
        out.append(m._replace(safe=name))
    return out


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


async def _resolve_destination(bot, destination_id: str):
    """The live channel-or-thread named by a STORED id. Either kind resolves
    the same way, which is what lets one column record whichever of the two
    carried a team's pools. Checked against the cache first, then a real
    fetch — the cache alone can miss a thread across a bot restart.

    Takes a real id, never None: "this team has no destination yet" is the
    caller's own state, not something to ask Discord about, and answering it
    here would collapse it into the same None as a failed lookup.

    Returns None for exactly one thing — `NotFound`, meaning Discord confirms
    the destination is gone. That is safe to treat as "start again": whatever
    was posted there went with it, so re-posting duplicates nothing.

    Every other `discord.HTTPException` propagates, including `Forbidden`.
    Forbidden is not "gone", it is "I cannot see it" — the destination may be
    perfectly alive with every pool already in it, and a bot that reads that
    as absent opens a second thread, re-posts everyone, and strands the
    first. The caller must abort the run and let the retry sort it out; a
    transient 5xx or timeout is the same story."""
    cid = int(destination_id)
    destination = bot.get_channel(cid)
    if destination is None:
        try:
            destination = await bot.fetch_channel(cid)
        except discord.NotFound:
            return None
    return destination


async def _posted_txt_filenames(bot, destination, limit: int | None = None,
                                oldest_first: bool = False,
                                stop_when: set[str] | None = None) -> set[str]:
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
    history = (destination.history(limit=limit, oldest_first=True) if oldest_first
               else destination.history(limit=limit))
    async for message in history:
        if message.author.id != bot_id:
            continue
        names.update(a.filename for a in message.attachments if a.filename.endswith(".txt"))
        if stop_when and stop_when <= names:
            break
    return names


async def _post_missing_players(
    destination,
    postable: list[_PostableMember],
    draft_data: dict,
    already_posted: set[str],
    send: Callable[..., Awaitable[None]] | None = None,
) -> tuple[bool, int]:
    """Post each postable member not already in `already_posted` to
    `destination` (a thread, or the channel on the no-thread fallback).
    Guards each send individually — one player's failure is logged and the
    loop continues rather than costing the others their pools.

    `send` overrides how one pool is written (default `_send_pool`); the open-pools
    path passes a partial that adds the team label and the combined pile. The skip
    rule, the error policy and the filename convention stay here either way.

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
            await (send or _send_pool)(destination, member, draft_data)
            sent += 1
        except Exception as e:
            logger.warning(
                f"[team-logs] failed to post pool for {member.name} ({member.dm_user_id}): {e}"
            )
            all_posted = False
    return all_posted, sent


def _is_permanent(error: Exception) -> bool:
    """Whether Discord is saying "never" rather than "not now".

    Forbidden is a missing permission and NotFound is a channel that no longer
    exists; neither improves by being retried for 72h. Everything else — 5xx,
    rate-limit exhaustion, a timeout, or an SSL/connection error from below
    discord.py — is a blip, and blips are what retries are for.
    """
    return isinstance(error, (discord.Forbidden, discord.NotFound))


async def _open_pools_thread(channel, friendly_id: str, player_count: int) -> _ThreadAttempt:
    """The newly-created pools thread, hanging off one summary message in the
    channel — or a no-thread attempt if Discord refused, either the summary send
    or the thread creation itself (typically missing Manage Threads).

    A missing thread sends the private path down its per-player fallback: losing
    the tidy grouping is acceptable, losing people's pools is not. The open path
    has no fallback and reads `permanent` instead.

    Catches Exception, not just HTTPException: an aiohttp connection/SSL error
    (seen repeatedly mid-upload in live runs) is not a discord.py exception, and
    letting it escape here would cost the whole reconciler run — including the
    private pools that are the actual deliverable — over one optional thread.
    """
    try:
        summary = await channel.send(
            content=f"📥 **Drafted pools** — {friendly_id} ({player_count} players)"
        )
    except Exception as e:
        logger.warning(
            f"[team-logs] could not post the pools summary message for {friendly_id}: {e}"
        )
        return _ThreadAttempt(None, _is_permanent(e))

    try:
        return _ThreadAttempt(await summary.create_thread(
            name=f"Drafted pools — {friendly_id}"[:DISCORD_THREAD_NAME_LIMIT],
            auto_archive_duration=THREAD_ARCHIVE_MAX_MINUTES,
        ), False)
    except Exception as e:
        logger.warning(
            f"[team-logs] thread creation failed for {friendly_id}: {e}"
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
        except Exception as delete_error:
            logger.warning(
                f"[team-logs] could not remove the orphaned pools summary for "
                f"{friendly_id}: {delete_error}"
            )
        return _ThreadAttempt(None, _is_permanent(e))


async def _tag_team_in_thread(thread, member_discord_ids: list[str], friendly_id: str) -> None:
    """Open the thread by mentioning the team, so it surfaces in their sidebar.

    Discord adds a mentioned member to the thread, and a thread you belong to
    is the one that shows up in the channel list rather than staying buried
    behind the summary message. So the mention is what makes this organised,
    not just polite.

    Posted plain and then EDITED to carry the mention, so it never notifies:
    a mention that arrives with a new message pings, the same mention edited
    into an existing one does not.

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
    plain = f"Your drafted pools for {friendly_id}:"
    try:
        await send_then_mention(thread, plain, f"{mentions} — your drafted pools for {friendly_id}:")
    except Exception as e:
        # Exception, not HTTPException: "must not cost anyone their pools" is
        # only true if it holds for every failure. The deck-image build and
        # each pool send guard the same way, as does the scouting starter.
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

    Whichever of the two carries the pools becomes this team's recorded
    destination, persisted via `persist_destination_id`. That is the
    load-bearing idea: a retry resumes into the SAME place, so pools are never
    split across a channel and a thread, and a team whose fallback already
    delivered never gets a second, empty thread opened for them later.

    The two are claimed on purpose asymmetrically. A new thread is recorded
    immediately, before anyone is posted, so a run that dies mid-way resumes
    into it instead of opening another. The channel is recorded only once a
    pool actually lands there, so a fallback that delivers nothing leaves the
    team eligible for a thread next tick instead of being pinned to the
    channel by a failure.

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
        logger.warning(
            f"[team-logs] no channel for a team with {len(postable)} postable "
            f"players in {friendly_id}; posting nothing for them this run")
        return False

    try:
        # "Nothing stored yet" is our own state, so it is answered here rather
        # than by a lookup that would report it as the same None as a failure.
        destination = (
            await _resolve_destination(bot, destination_id) if destination_id else None
        )
    except discord.HTTPException as e:
        # A destination is stored but Discord would not confirm it is gone:
        # a transient 5xx, a timeout, or a Forbidden that means "I cannot see
        # it" rather than "it does not exist". Abort rather than risk opening
        # a second thread alongside a first one that may be perfectly alive,
        # holding the very pools we are about to re-post.
        logger.warning(
            f"[team-logs] could not resolve the stored pools destination for {friendly_id}, "
            f"aborting this run rather than risk a second thread: {e}"
        )
        return False

    if destination is None:
        # Nothing recorded yet, so this team has never had a pool delivered.
        thread = (await _open_pools_thread(channel, friendly_id, len(postable))).thread
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
    # thread holds nothing but the team tag and one message per player, so
    # scanning it whole is both cheap and exact.
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
            logger.warning(f"post_team_logs: no session row for {session_id}")
            return False
        if ds.team_logs_posted_at is not None:
            return True
        draft_data = ds.draft_data
        if not draft_data:
            logger.warning(
                f"post_team_logs: session {session_id} has no draft_data yet; nothing to post")
            return False
        team_a = list(ds.team_a or [])
        team_b = list(ds.team_b or [])
        channel_ids = list(ds.channel_ids or [])
        sign_ups = dict(ds.sign_ups or {})
        guild_id = ds.guild_id
        friendly_id = ds.friendly_id or ds.draft_id or session_id
        team_a_destination_id = ds.team_a_pools_destination_id
        team_b_destination_id = ds.team_b_pools_destination_id
        tournament_match_id = ds.tournament_match_id
        draft_chat_channel = ds.draft_chat_channel
        open_destination_id = ds.open_pools_destination_id
        # Always real names: tournament_linking only links a draft whose team names
        # score against the participants, so neither can be empty by the time we're here.
        team_a_label, team_b_label = ds.team_a_name, ds.team_b_name

    guild = bot.get_guild(int(guild_id)) if guild_id else None
    if guild is None:
        logger.warning(
            f"post_team_logs: guild {guild_id} not resolvable for session {session_id}")
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

    # A tournament match is played with pools open, so both teams' pools also go to
    # one thread in the shared draft chat. Deliberately after the private threads:
    # those are the deliverable each team imports from, and a failure to open the
    # shared thread must not cost a team its own pools.
    open_all_posted = True
    if tournament_match_id is not None:
        open_all_posted = await _post_open_pools(
            bot, guild.get_channel(int(draft_chat_channel)) if draft_chat_channel else None,
            open_destination_id, friendly_id,
            [(team_a_label, team_a), (team_b_label, team_b)],
            mapping, draft_data, sign_ups,
            persist_destination_id=partial(_persist_destination_id,
                                           "open_pools_destination_id"),
        )

    if not (red_all_posted and blue_all_posted and open_all_posted):
        # Some player(s) are still missing a pool (a send failure, or a
        # thread-creation refusal whose fallback pass also failed). Leave
        # team_logs_posted_at unset so the reconciler retries and posts only
        # the stragglers next tick.
        logger.warning(
            f"post_team_logs: not every player posted for session {session_id} "
            f"(red={'ok' if red_all_posted else 'incomplete'}, "
            f"blue={'ok' if blue_all_posted else 'incomplete'}, "
            f"open={'ok' if open_all_posted else 'incomplete'}); "
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


# --- open pools (tournament matches) ---------------------------------------------

async def _send_open_pool(destination, member: _PostableMember, draft_data: dict,
                          *, team_label: str, header: str | None = None) -> None:
    """One player's pool for an OPEN thread: same `.txt` deliverable as the private
    posts, but labelled with the team and imaged as a single pile.

    `header` heads the team's group. It rides on this post rather than going out as
    its own message so that it cannot be posted twice (see _post_open_pools) and
    cannot fail on its own.

    The pile is built with the whole pool as `main` and an empty sideboard on purpose.
    PileImageBuilder buckets main and side into separate groups, which is the right
    read when a pool is private and the built deck is the story; with pools open the
    split is noise — an opponent wants to see everything that was drafted, in one
    screenshot.
    """
    files = [discord.File(io.BytesIO(member.pool.encode("utf-8")), filename=f"{member.safe}.txt")]
    try:
        split = split_decklist(draft_data, member.dm_user_id)
        whole_pool = list(split.get("main") or []) + list(split.get("side") or [])
        image = await PileImageBuilder().build(whole_pool, [], draft_data.get("carddata", {}))
        if image:
            files.append(discord.File(io.BytesIO(image.getvalue()), filename=f"{member.safe}.jpg"))
    except Exception as e:
        logger.warning(f"[open-pools] deck image failed for {member.name}: {e}")

    line = f"**{member.name}** — {team_label} ({member.pool.count(chr(10)) + 1} cards):"
    await destination.send(content=f"{header}\n{line}" if header else line, files=files)


async def _post_open_pools(
    bot,
    channel,
    destination_id: str | None,
    friendly_id: str,
    teams: list[tuple[str, list[str]]],
    mapping: dict[str, str],
    draft_data: dict,
    sign_ups: dict,
    persist_destination_id: Callable[[str], Awaitable[None]],
) -> bool:
    """Both teams' pools in ONE thread off the shared draft chat, grouped by team.

    Only for tournament matches, which are played with pools open. The private
    per-team threads still happen — this is additional, and deliberately so: the
    private thread is where a team imports its own `.txt` from, and taking it away
    would trade a working habit for a tidier channel list.

    `teams` is [(team_label, [discord_ids]), …] in board order; the label heads that
    team's group and is repeated on each pool, so a post that is linked or scrolled
    to in isolation still says which side it belongs to.

    Resume works exactly like the per-team path: the thread is claimed before anyone
    is posted, and players already carrying a posted `.txt` are skipped — so a retry
    after a partial run finishes the stragglers instead of doubling the thread.
    """
    groups = [(label, members) for label, ids in teams
              if (members := _postable_members(ids, mapping, draft_data, sign_ups))]
    if not groups:
        return True

    # _postable_members dedupes safe names WITHIN a team, which is all the private
    # threads need — each team has its own destination. This thread merges both, so
    # two OPPONENTS whose names sanitise alike (say 'Bob!' and 'Bob?') would still
    # share Bob.txt here. They would both post on a first run, but the posted-check
    # reads filenames, so any retry would see one Bob.txt and skip the other's pool
    # for good. Re-dedupe over the set that actually shares this destination.
    merged = _dedupe_safe_names([m for _, members in groups for m in members])
    resolved, cursor = [], 0
    for label, members in groups:
        resolved.append((label, merged[cursor:cursor + len(members)]))
        cursor += len(members)
    groups = resolved
    if channel is None:
        return False

    try:
        destination = (
            await _resolve_destination(bot, destination_id) if destination_id else None
        )
    except discord.HTTPException as e:
        # Same reasoning as _post_pools_for_team: a stored destination Discord won't
        # confirm is gone must not become a second thread beside a live first one.
        logger.warning(
            f"[open-pools] could not resolve the stored destination for {friendly_id}, "
            f"aborting rather than risk a second thread: {e}"
        )
        return False

    if destination is None:
        total = sum(len(members) for _, members in groups)
        attempt = await _open_pools_thread(channel, friendly_id, total)
        destination = attempt.thread
        if destination is None:
            # No fallback to the channel (unlike the private path): the shared draft
            # chat is where the match is being played, and spraying every pool into
            # it unthreaded is worse than not posting them at all.
            #
            # Reported as SUCCESS on purpose. The private per-team pools are the
            # deliverable; these are additional. Returning False here would leave
            # team_logs_posted_at unstamped, which keeps the session in the
            # reconciler's 72h window — ~4,320 ticks, each re-entering here to post
            # a summary into the live match channel and delete it again. A draft
            # chat the bot cannot thread in does not get better by being retried
            # 4,320 times, so this is terminal. A send failure AFTER the thread
            # exists still returns False: by then the destination is persisted, so
            # the retry resumes into it and posts only the stragglers.
            if not attempt.permanent:
                # A blip, not a refusal. Returning True here would stamp the
                # session complete with no destination recorded and no tick ever
                # coming back — the open pools lost silently and for good.
                logger.warning(
                    f"[open-pools] could not open the shared pools thread for "
                    f"{friendly_id} (transient); leaving the run incomplete to retry"
                )
                return False
            logger.warning(
                f"[open-pools] the shared pools thread for {friendly_id} was refused; "
                "the private team pools stand on their own, not retrying"
            )
            return True
        await persist_destination_id(str(destination.id))

    # OLDEST-first, and bounded by finding what it is looking for rather than by a
    # message count. This thread hangs off the shared draft chat for a match played
    # with pools open, so opponents talking in it is the point — it is the one pools
    # destination where arbitrary chatter is expected. Any fixed window can be
    # exceeded by conversation landing between two pools while a slow image upload is
    # in flight, which hides the later pool and re-posts it; reading from the start
    # until every expected filename is accounted for cannot be fooled that way.
    already = await _posted_txt_filenames(
        bot, destination, oldest_first=True,
        stop_when={f"{m.safe}.txt" for _, members in groups for m in members})
    all_posted = True
    for label, members in groups:
        pending = [m for m in members if f"{m.safe}.txt" not in already]
        if not pending:
            continue
        # The team header rides on the group's first pool instead of being its own
        # message, so it is idempotent for free: a retry finishing a straggler is
        # never the group's first post, so it cannot drop a second header into the
        # middle of that team's pools. Tracking it by message content instead would
        # mean a header-aware history scan; tracking it by "is the group untouched"
        # gets a one-player team wrong, because its only pool failing makes the
        # group look untouched again.
        head = [f"__**{label}**__" if len(pending) == len(members) else None]

        async def _send(dest, member, data, _head=head, _label=label):
            await _send_open_pool(dest, member, data, team_label=_label, header=_head[0])
            _head[0] = None      # cleared only on success, so a failed first post
                                 # hands the header to whoever posts next

        # The same helper the private path uses, so the skip-already-posted rule, the
        # per-member error policy, and the filename convention have ONE home. A copy
        # of this loop shipped catching only discord.HTTPException and had to be
        # re-fixed when an e2e run died on an aiohttp ClientOSError mid-upload —
        # a lesson _post_missing_players had already learned.
        posted, _ = await _post_missing_players(
            destination, pending, draft_data, already, send=_send)
        all_posted = all_posted and posted
    return all_posted
