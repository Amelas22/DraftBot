"""Drafts run on the self-hosted Draftmancer, from queue to finished log.

DraftBot never sits in a hosted draft. It creates the session through the
control API when the queue opens, keeps the expected players and the seating in
step with Discord, and polls status to drive the draft channel: the status
message, Draftmancer's in-app ready check (after the Discord one has formed the
teams), the start, and a pause while a seat is empty mid-draft. The end arrives
as a pushed log -- or the reconciler's pull -- and runs finish_draft, which
builds rooms at most once.

Everything the poller acts on is re-read from the database and the status
endpoint every tick, so a restarted bot picks up where it was. Who paused the
table, its notice, and the seats an admin gave to a bot are in the database too:
a restart neither lifts a player's /pause nor pauses for a bot-played seat. What
stays in memory is only a running ready check's messages; losing it on a restart
costs at most one repeated message.

Discord commands on a hosted draft (/pause, /unpause, /scrap, /ready,
/release_draft_logs, /replace_seat, Update Cube) are API calls made here, under
the same per-draft lock as the poller's tick, so a command and a tick never act
on one draft at once.

The fallback (route "fallback") is a plain session on the public draftmancer.com
with no bot inside -- the state /mutiny leaves a manager draft in. A draft goes
there on its own only when its hosted session cannot be created; once one
exists, a failing server is retried, the draft channel told, and the admins
alerted once per outage, and an admin decides with /mutiny, which closes the
hosted session first. Its log is fetched from draftmancer.com afterwards, when
the players' settings allow it.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import discord
from loguru import logger
from sqlalchemy import select, update

from config import (draftmancer_api_token, draftmancer_api_url, get_draftmancer_session_url,
                    hosted_draftmancer_enabled, public_draftmancer_session_url,
                    public_draftmancer_url, scheduler_base_url)
from cube_views.pack_options import DEFAULT_CARDS_PER_PACK, DEFAULT_PACKS_PER_PLAYER
from database.db_session import db_session
from helpers.hosted_seats import discord_ids_by_seat, seat_list, seat_names
from models.draft_session import DraftSession
from notification_service import send_dm, send_ready_check_dms
from services.draft_end import DraftLog, finish_draft, publish_draft_log
from services.draft_messages import (
    READY_CHECK_PASSED,
    READY_CHECK_TIMEOUT_SECONDS,
    START_DELAY_SECONDS,
    format_status_message,
    ready_check_deadline_text,
    ready_check_left_text,
    ready_check_not_ready_text,
    ready_check_reset_text,
    ready_check_text,
    ready_check_timed_out_text,
    session_status,
    update_or_send_message,
)
from services.draft_setup_manager import (
    DISCONNECT_NOTICE_DELAY,
    DRAFT_LOG_UNLOCK_TIMER_MINUTES,
    DRAFT_PICK_TIMER_SECONDS,
)
from services.hosted_draftmancer import (
    HostedCubeImportFailed,
    Outcome,
    HostedDraftmancer,
    HostedLogStopped,
    HostedRefused,
    HostedSeat,
    HostedStatus,
    HostedUnavailable,
    build_session_spec,
    fetch_public_log,
)

ROUTE = "hosted"
FALLBACK_ROUTE = "fallback"
POLL_INTERVAL_SECONDS = 5
# Drafts opened longer ago than this are not polled: by then a hosted draft has
# ended (or been scrapped, abandoned or cancelled), and asking the server about
# every one of them on each restart would only add calls and outage notices.
POLL_WINDOW = timedelta(hours=24)
# A player who answered "not ready" cannot answer the same check again (the
# in-app prompt is one-shot), so a fresh one is asked for after this pause.
NOT_READY_RETRY_SECONDS = 30
WEBHOOK_PATH = "/hosted/draft-ended"

# Who paused a hosted draft (draft_sessions.hosted_paused_by). The poller lifts
# only its own pause, once every seat is back; a player's /pause waits for /unpause.
BOT_PAUSE = "bot"
HUMAN_PAUSE = "human"

# Only the mentions this module builds may ping: a display name is free text.
_PING_USERS_ONLY = discord.AllowedMentions(everyone=False, roles=False, users=True)


# --- the client -------------------------------------------------------------------

_client: HostedDraftmancer | None = None


def hosted_client() -> HostedDraftmancer:
    """The one client this process uses, made on first use from the environment."""
    global _client
    if _client is None:
        _client = HostedDraftmancer(draftmancer_api_url(), draftmancer_api_token(),
                                    on_outcome=_observe_api)
    return _client


async def close_hosted_client() -> None:
    global _client
    client, _client = _client, None
    if client is not None:
        await client.close()


def log_webhook_url() -> str:
    """Where the hosted server pushes a finished log: the scheduler web app."""
    return scheduler_base_url() + WEBHOOK_PATH


def _now() -> float:
    return time.time()


# --- queue, sign-ups, teams ---------------------------------------------------------

# Why a draft runs on the fallback; each has its own channel notice.
FallbackReason = Literal["unreachable", "cube", "refused", "mutiny", "mutiny-unstopped"]


async def open_draft_room(draft_session: Any, channel: Any) -> str:
    """Open a new queue's Draftmancer room: hosted, or the fallback when that fails.

    Returns the route taken. A server already down for OUTAGE_ALERT_AFTER_SECONDS
    is not asked: the attempt would only spend the interaction's few seconds on
    a timeout before falling back anyway.
    """
    reason: FallbackReason | None = "unreachable"
    if not _known_down():
        reason = await _create_hosted(draft_session)
    if reason is None:
        return ROUTE
    await open_fallback_draft(draft_session, reason, channel=channel)
    return FALLBACK_ROUTE


async def _create_hosted(draft_session: Any) -> FallbackReason | None:
    """Create the session and record the route; None on success, else why it failed."""
    spec = build_session_spec(
        draft_id=draft_session.draft_id,
        players=seat_list(draft_session.sign_ups or {}),
        cube_id=draft_session.cube,
        packs_per_player=draft_session.packs_per_player or DEFAULT_PACKS_PER_PLAYER,
        cards_per_pack=draft_session.cards_per_pack or DEFAULT_CARDS_PER_PACK,
        pick_timer=DRAFT_PICK_TIMER_SECONDS,
        unlock_timer_minutes=DRAFT_LOG_UNLOCK_TIMER_MINUTES,
        log_webhook=log_webhook_url(),
    )
    try:
        await hosted_client().create(spec)
    except HostedCubeImportFailed as e:
        logger.error(f"The hosted server could not import cube {draft_session.cube} "
                     f"for {draft_session.session_id}: {e}")
        return "cube"
    except HostedUnavailable as e:
        logger.error(f"Could not reach the hosted server for {draft_session.session_id}: {e}")
        return "unreachable"
    except HostedRefused as e:
        logger.error(f"The hosted server refused to create {draft_session.session_id}: {e}")
        return "refused"
    try:
        await _store(draft_session.session_id, draftmancer_route=ROUTE,
                     draft_link=get_draftmancer_session_url(draft_session.draft_id))
    except Exception:
        # A row left without the route would have no poller, and a restart
        # would put a manager into a draft that has none: close the room and
        # take the fallback, whose own write records a route.
        logger.opt(exception=True).error(
            f"Could not record the hosted route of {draft_session.session_id}; "
            f"closing its hosted session")
        await _stop_quietly(hosted_client(), draft_session.draft_id)
        return "refused"
    logger.info(f"Opened hosted session DB{draft_session.draft_id} for {draft_session.session_id}")
    return None


async def open_fallback_draft(draft_session: Any, reason: str, *, channel: Any = None,
                              requested_by: str | None = None) -> None:
    """Move the draft to a new plain session on the public draftmancer.com.

    A new draft id and link, the route "fallback", and none of the hosted
    route's state. DraftBot never joins it -- no manager is built -- and the
    players run the room; rooms and pairings come from the board's "Create
    Rooms & Post Pairings" button. `channel` is told why, how to run it, and,
    once teams exist, every player's new link. `draft_session` itself is not
    changed (it may belong to a caller's open transaction): read the row again.
    """
    new_id = _new_draft_id()
    values: dict[str, Any] = {
        "draft_id": new_id,
        "draft_link": public_draftmancer_session_url(new_id),
        "draftmancer_route": FALLBACK_ROUTE,
        "hosted_paused_by": None,
        "hosted_pause_notice_id": None,
        "hosted_bot_seats": None,
    }
    await _store(draft_session.session_id, **values)
    logger.warning(f"{draft_session.session_id} runs on draftmancer.com as DB{new_id} ({reason})")
    if channel is None:
        return
    row = await DraftSession.get_by_session_id(draft_session.session_id)
    if row is None:
        return
    try:
        await channel.send(_fallback_text(row, reason, requested_by), allowed_mentions=_PING_USERS_ONLY)
    except Exception as e:
        logger.error(f"Could not post the fallback notice of {draft_session.session_id}: {e}")


_MUTINY_GIF = ("https://tenor.com/view/mutiny-jack-sparrow-pirates-pirates-of-the-caribbean-"
               "i-wish-to-report-a-mutiny-gif-26531174")


def _fallback_text(row: Any, reason: str, requested_by: str | None) -> str:
    cube = f"**{row.cube}**" if row.cube else "the cube"
    where = "on draftmancer.com instead, without DraftBot in the room"
    head = {
        "unreachable": f"⚠️ **DraftBot's drafting server couldn't be reached**, so this draft runs {where}.",
        "cube": f"⚠️ **The drafting server couldn't import the cube {cube} from CubeCobra**, so "
                f"this draft runs {where}. Import it yourselves in Draftmancer, as below.",
        "refused": f"⚠️ **The drafting server couldn't open this draft**, so it runs {where}.",
        "mutiny": f"{_MUTINY_GIF}\n\n🔄 **Mutiny!** {requested_by or 'A player'} moved this draft "
                  f"to draftmancer.com, without DraftBot in the room. The drafting server's "
                  f"room is closed.",
        "mutiny-unstopped": f"{_MUTINY_GIF}\n\n🔄 **Mutiny!** {requested_by or 'A player'} moved "
                            f"this draft to draftmancer.com, without DraftBot in the room.\n"
                            f"⚠️ The drafting server couldn't be reached to close its room, which "
                            f"may still be open if it comes back. Don't draft there.",
    }.get(reason, f"⚠️ This draft runs {where}.")
    packs = row.packs_per_player or DEFAULT_PACKS_PER_PLAYER
    cards = row.cards_per_pack or DEFAULT_CARDS_PER_PACK
    seating = ("seat everyone in the team order posted here" if row.session_stage
               else "once teams are posted here, seat everyone in that order")
    how = (
        f"**Running the room:** whoever opens their link first owns it. In Draftmancer's "
        f"settings, import {cube} from CubeCobra ({packs} packs of {cards} cards), turn on "
        f"Team Draft, and {seating} with random seating **off**."
    )
    if not row.session_stage:
        how += " After the draft, press **Create Rooms & Post Pairings** on the draft board."
    parts = [head, how]
    if not row.session_stage and row.sign_ups:
        # They signed up while the server was being asked, and were handed its link.
        parts.append("Signed up already? The link you were given points at the old room; "
                     "the links posted with the teams are the right ones.")
    links = [f"<@{discord_id}> — [your new draft link]({link})"
             for discord_id, name in (row.sign_ups or {}).items()
             if row.session_stage and (link := row.get_draft_link_for_user(name, user_id=discord_id))]
    if links:
        parts.append("**Any draft link you were given before this message no longer works.**\n"
                     + "\n".join(links))
    return "\n\n".join(parts)


async def _hosted_row(session_id: str) -> Any:
    if not hosted_draftmancer_enabled():
        return None
    row = await DraftSession.get_by_session_id(session_id)
    return row if row is not None and row.draftmancer_route == ROUTE else None


async def on_signups_changed(session_id: str) -> None:
    """Send the hosted session the current sign-ups as its expected players.

    Never raises: a failed update is retried by the poller, which compares the
    seats with the sign-ups on every tick.
    """
    try:
        if _known_down():
            return  # Not worth the click's wait: the poller re-syncs once it is back.
        row = await _hosted_row(session_id)
        if row is None:
            return
        await hosted_client().set_players(row.draft_id, seat_list(row.sign_ups or {}))
    except Exception as e:
        logger.warning(f"Could not update the hosted players of {session_id}: {e}")


async def on_teams_formed(session_id: str, seating: list[str]) -> None:
    """Seat the hosted session in the order the teams were drawn. Never raises.

    `seating` is seat names (helpers.hosted_seats.seat_list of the reordered
    sign-ups), not display names.
    """
    try:
        row = await _hosted_row(session_id)
        if row is None:
            return
        client = hosted_client()
        # The players first: the seating must name exactly the seats there are.
        await client.set_players(row.draft_id, seating)
        await client.set_seating(row.draft_id, seating)
    except Exception as e:
        logger.warning(f"Could not seat the hosted session of {session_id}: {e}")


# --- what the poller remembers ---------------------------------------------------------

@dataclass
class _ReadyCheck:
    message_id: str | None
    deadline_message_id: str | None
    deadline: float
    shown: int = 0


@dataclass
class _Tracked:
    status_key: tuple[tuple[str, ...], tuple[str, ...]] | None = None
    status_message_id: str | None = None
    ready_check: _ReadyCheck | None = None
    # After a failed check: "all-ready" (start again once every seat reads
    # ready, as after a timeout today) or the time a new check may be asked for.
    retry: str | float | None = None
    # The seats the pause notice names. None after a restart: the notice is
    # re-rendered once from the current seats.
    paused_for: list[str] | None = None


_TRACKED: dict[str, _Tracked] = {}
# Drafts whose hosted session has ended, stopped or gone: no more polling.
_DONE: set[str] = set()
_END_LOCKS: dict[str, asyncio.Lock] = {}
_FINISHING: dict[str, asyncio.Task[None]] = {}
# One per draft: a poll tick and a command never act on the same draft at once.
_DRAFT_LOCKS: dict[str, asyncio.Lock] = {}
# Drafts the reconciler has learnt will never have a log: stopped, or lost.
_UNPULLABLE: set[str] = set()


def forget_all() -> None:
    """Drop everything remembered (tests, and what a restart loses)."""
    global _HEALTH
    _TRACKED.clear()
    _DONE.clear()
    _UNPULLABLE.clear()
    _HEALTH = _Health()


def _draft_lock(session_id: str) -> asyncio.Lock:
    return _DRAFT_LOCKS.setdefault(session_id, asyncio.Lock())


# --- the poller --------------------------------------------------------------------------

async def run_hosted_poller(bot: discord.Client) -> None:
    """Poll every hosted draft not yet ended, forever. A failed tick is logged and retried."""
    logger.info("Hosted draft poller started")
    try:
        while True:
            try:
                await poll_hosted_drafts(bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.opt(exception=True).error("Hosted draft poll failed; retrying next tick")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        await close_hosted_client()


async def poll_hosted_drafts(bot: discord.Client) -> None:
    """One tick: every hosted draft that has not ended, concurrently."""
    if not hosted_draftmancer_enabled():
        return
    # Naive local time, as draft_start_time is stored (and reconnect_drafts compares).
    since = datetime.now() - POLL_WINDOW
    async with db_session() as session:
        rows = list((await session.execute(
            select(DraftSession).where(
                DraftSession.draftmancer_route == ROUTE,
                DraftSession.logs_captured_at.is_(None),
                DraftSession.draft_start_time >= since,
                # `!=` alone would drop a queue's NULL stage.
                (DraftSession.session_stage.is_(None))
                | (DraftSession.session_stage != "abandoned"),
            ))).scalars())
    rows = [r for r in rows if r.session_id not in _DONE]
    results = await asyncio.gather(*(_poll_draft(bot, row) for row in rows),
                                   return_exceptions=True)
    for row, result in zip(rows, results):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            logger.opt(exception=result).error(f"Polling hosted draft {row.session_id} failed")
    if not rows and _HEALTH.failing_since is not None:
        # No draft's poll will say whether it is back -- the failure may have
        # been a create that fell back -- so ask the server itself.
        try:
            await hosted_client().ping()
        except HostedUnavailable:
            pass
    await _report_health(bot, rows)


async def _poll_draft(bot: discord.Client, row: Any) -> None:
    async with _draft_lock(row.session_id):
        # Read again under the lock: a command may have paused, resumed or moved
        # the draft while this tick waited for it.
        fresh = await DraftSession.get_by_session_id(row.session_id)
        if fresh is not None and fresh.draftmancer_route == ROUTE:
            await _poll_draft_locked(bot, fresh)


async def _poll_draft_locked(bot: discord.Client, row: Any) -> None:
    try:
        status = await hosted_client().status(row.draft_id)
    except HostedUnavailable as e:
        logger.warning(f"Hosted status for {row.session_id} unavailable: {e}")
        return
    except HostedRefused as e:
        if e.status == 404:
            logger.warning(f"Hosted session for {row.session_id} no longer exists; not polling it")
            _forget(row.session_id)
            return
        # A refused token: the admins hear once per incident (_report_auth).
        logger.warning(f"Hosted status for {row.session_id} refused: {e}")
        return
    st = _TRACKED.setdefault(row.session_id, _Tracked())
    channel = bot.get_channel(int(row.draft_channel_id)) if row.draft_channel_id else None
    if status.stage == "gathering":
        await _gathering(bot, row, status, st, channel)
    elif status.stage == "drafting":
        await _drafting(row, status, st, channel)
    else:
        # Ended or stopped: the pushed log (or the reconciler) finishes it.
        _forget(row.session_id)


def _forget(session_id: str) -> None:
    _DONE.add(session_id)
    _TRACKED.pop(session_id, None)


async def _gathering(bot: discord.Client, row: Any, status: HostedStatus, st: _Tracked,
                     channel: Any) -> None:
    expected = seat_list(row.sign_ups or {})
    if [s.name for s in status.seats] != expected:
        # A missed sign-up update, or one made while the API was away. Setting the
        # players clears the in-app ready check, so ours goes with it.
        await hosted_client().set_players(row.draft_id, expected)
        if st.ready_check is not None:
            changed = sorted(set(expected) ^ {s.name for s in status.seats})
            await _end_ready_check(channel, st, ready_check_left_text(changed[0]) if changed
                                   else ready_check_reset_text())
        st.retry = None
        return
    if not row.session_stage or channel is None:
        return  # No teams yet: nobody has been given a link.
    await _report_status(row, status, st, channel)
    await _ready_check_step(bot, row, status, st, channel)


async def _report_status(row: Any, status: HostedStatus, st: _Tracked, channel: Any) -> None:
    present = tuple(sorted(s.name for s in status.seats if s.connected))
    missing = tuple(sorted(s.name for s in status.seats if not s.connected))
    if (present, missing) == st.status_key:
        return
    text = format_status_message(session_status(present, missing))
    message = await update_or_send_message(channel, st.status_message_id or row.status_message_id,
                                           content=text)
    if message is None:
        return
    st.status_key = (present, missing)
    if str(message.id) != row.status_message_id:
        st.status_message_id = str(message.id)
        await _store(row.session_id, status_message_id=str(message.id))
        row.status_message_id = str(message.id)


async def _ready_check_step(bot: discord.Client, row: Any, status: HostedStatus, st: _Tracked,
                            channel: Any) -> None:
    seats = status.seats
    away = [s.name for s in seats if not s.connected]
    rc = st.ready_check
    if rc is not None:
        await _running_ready_check(row, seats, away, st, rc, channel)
        return
    if away:
        st.retry = None  # A leave resets: a new check runs once everyone is back.
        return
    if st.retry == "start":
        # The check passed but the start did not reach the server: try it again
        # quietly while every seat still reads ready.
        if all(s.ready == "ready" for s in seats):
            await _start(row, st, channel, announce=False)
            return
        st.retry = None
    if st.retry == "all-ready":
        if not all(s.ready == "ready" for s in seats):
            return
    elif isinstance(st.retry, float) and _now() < st.retry:
        return
    st.retry = None
    await _open_ready_check(bot, row, len(seats), st, channel)


async def _running_ready_check(row: Any, seats: list[HostedSeat], away: list[str], st: _Tracked,
                               rc: _ReadyCheck, channel: Any) -> None:
    if away:
        await _end_ready_check(channel, st, ready_check_left_text(away[0]))
        st.retry = None
        return
    declined = [s.name for s in seats if s.ready == "not-ready"]
    if declined:
        await _end_ready_check(channel, st, ready_check_not_ready_text(declined, NOT_READY_RETRY_SECONDS))
        st.retry = _now() + NOT_READY_RETRY_SECONDS
        return
    ready = sum(1 for s in seats if s.ready == "ready")
    if ready == len(seats):
        await _start(row, st, channel)
        return
    if ready != rc.shown:
        rc.shown = ready
        await update_or_send_message(channel, rc.message_id, content=ready_check_text(ready, len(seats)))
    if _now() >= rc.deadline:
        waiting = [s.name for s in seats if s.ready != "ready"]
        await _end_ready_check(channel, st, ready_check_timed_out_text(READY_CHECK_TIMEOUT_SECONDS, waiting))
        st.retry = "all-ready"


async def _open_ready_check(bot: discord.Client, row: Any, total: int, st: _Tracked,
                            channel: Any) -> None:
    await hosted_client().ready_check(row.draft_id)
    deadline = _now() + READY_CHECK_TIMEOUT_SECONDS
    message = await channel.send(ready_check_text(0, total, first=True))
    deadline_message = await channel.send(ready_check_deadline_text(int(deadline)))
    st.ready_check = _ReadyCheck(str(message.id), str(deadline_message.id), deadline)
    await _store(row.session_id, ready_check_message_id=str(message.id))
    logger.info(f"Hosted ready check started for {row.session_id}")
    try:
        await send_ready_check_dms(
            bot_or_client=bot, draft_session=row, guild_id=str(channel.guild.id),
            channel_id=str(channel.id), channel_name=channel.name, guild_name=channel.guild.name)
    except Exception as e:
        logger.error(f"Ready check DMs failed for {row.session_id}: {e}")


async def _end_ready_check(channel: Any, st: _Tracked, text: str | None) -> None:
    """Close the running check: say why on its message, and drop its deadline line."""
    rc, st.ready_check = st.ready_check, None
    if rc is None or channel is None:
        return
    if text is not None:
        await update_or_send_message(channel, rc.message_id, content=text)
    await _delete_message(channel, rc.deadline_message_id)


async def _start(row: Any, st: _Tracked, channel: Any, *, announce: bool = True) -> None:
    if announce:
        await _end_ready_check(channel, st, None)
        await channel.send(READY_CHECK_PASSED)
        await asyncio.sleep(START_DELAY_SECONDS)
    st.retry = "start"
    try:
        missing = await hosted_client().start(row.draft_id)
    except HostedUnavailable as e:
        logger.warning(f"Hosted start of {row.session_id} unavailable; retrying next tick: {e}")
        return
    except HostedRefused as e:
        logger.warning(f"Hosted start of {row.session_id} refused: {e}")
        st.retry = None
        return
    st.retry = None
    if missing:
        # Someone dropped in the last seconds; a new check runs once they are back.
        logger.info(f"Hosted start of {row.session_id} refused; waiting on {missing}")
        await channel.send(ready_check_left_text(missing[0]))
        return
    logger.info(f"Hosted draft {row.session_id} started")


async def _drafting(row: Any, status: HostedStatus, st: _Tracked, channel: Any) -> None:
    st.ready_check = None
    if row.hosted_paused_by == HUMAN_PAUSE:
        st.paused_for = None
        return  # A player's /pause: only /unpause lifts it.
    seats = _human_seats(row, status.seats)
    away = _away_past_grace(seats)
    paused = row.hosted_paused_by == BOT_PAUSE
    if away:
        if not paused:
            await hosted_client().pause(row.draft_id)
            await _store_on(row, hosted_paused_by=BOT_PAUSE)
            st.paused_for = away
            logger.info(f"Paused hosted draft {row.session_id}: {away} disconnected")
            if channel is not None:
                notice = await channel.send(_paused_text(row, away), allowed_mentions=_PING_USERS_ONLY)
                await _store_on(row, hosted_pause_notice_id=str(notice.id))
        elif away != st.paused_for:
            # Someone else dropped or came back -- or a restart forgot whom the notice names.
            st.paused_for = away
            if channel is not None:
                await _edit_notice(row, channel, _paused_text(row, away))
        return
    if any(not s.connected for s in seats):
        return  # Only a blip so far, or still paused while someone's blip passes.
    if paused:
        await hosted_client().resume(row.draft_id)
        logger.info(f"Resumed hosted draft {row.session_id}: every seat is back")
        if channel is not None:
            await _edit_notice(row, channel, _resumed_text(row, st.paused_for or []))
        await _store_on(row, hosted_paused_by=None, hosted_pause_notice_id=None)
        st.paused_for = None


def _human_seats(row: Any, seats: list[HostedSeat]) -> list[HostedSeat]:
    """The seats a person plays: the server keeps reporting a bot-played seat
    disconnected, and it must never hold the table for it."""
    bots = set(row.hosted_bot_seats or [])
    return [s for s in seats if s.name not in bots]


def _away_past_grace(seats: list[HostedSeat]) -> list[str]:
    """Seats disconnected for at least DISCONNECT_NOTICE_DELAY, as the manager waits.

    Short drops are common and self-correcting; pausing the table and posting
    for each would train players to ignore the notice that matters. A seat
    with no disconnect time cannot be measured, so it counts as gone.
    """
    now = _now()
    return sorted(
        s.name for s in seats
        if not s.connected and (s.disconnected_since is None
                                or now - s.disconnected_since >= DISCONNECT_NOTICE_DELAY))


def _mentions(row: Any, seats: list[str]) -> list[str]:
    """Each seat as its player's mention: seat names are unique, display names are not."""
    ids = discord_ids_by_seat(row.sign_ups or {})
    return [f"<@{ids[name]}>" if name in ids else name for name in seats]


def _paused_text(row: Any, away: list[str]) -> str:
    who = ", ".join(_mentions(row, away))
    links = "that player's link" if len(away) == 1 else "the missing players' links"
    return (
        f"⏸️ **Draft paused** — {who} dropped out of Draftmancer, so the table is "
        f"paused and the seat is held for them.\n\n"
        f"They can rejoin with their own link. If they can't come back, a substitute "
        f"can open {links} from the draft board and take over the seat and its pool.\n\n"
        f"The draft resumes on its own once every seat is connected again."
    )


def _resumed_text(row: Any, away: list[str]) -> str:
    if not away:
        return "▶️ **Draft resumed** — every seat is connected again."
    who = ", ".join(_mentions(row, away))
    return f"▶️ **Draft resumed** — every seat is connected again ({who} dropped out for a while)."


async def _edit_notice(row: Any, channel: Any, text: str) -> None:
    message = await update_or_send_message(channel, row.hosted_pause_notice_id, content=text)
    if message is not None and str(message.id) != row.hosted_pause_notice_id:
        await _store_on(row, hosted_pause_notice_id=str(message.id))


async def _delete_message(channel: Any, message_id: str | None) -> None:
    if not message_id:
        return
    try:
        message = await channel.fetch_message(int(message_id))
        await message.delete()
    except Exception as e:
        logger.debug(f"Could not delete message {message_id}: {e}")


async def _store(session_id: str, **values: Any) -> None:
    async with db_session() as session:
        await session.execute(update(DraftSession)
                              .where(DraftSession.session_id == session_id).values(**values))


async def _store_on(row: Any, **values: Any) -> None:
    """Write the values and keep the tick's row in step with them."""
    await _store(row.session_id, **values)
    for name, value in values.items():
        setattr(row, name, value)


# --- commands --------------------------------------------------------------------------
#
# Each returns what happened, in a word the cog turns into a reply; none raises
# for an API that is down or says no.

PauseResult = Literal["paused", "already", "not-drafting", "unreachable", "gone"]
ResumeResult = Literal["resumed", "not-drafting", "unreachable", "gone"]
StopResult = Literal["stopped", "ended", "unreachable", "gone"]
ReplaceResult = Literal["replaced", "not-in-draft", "refused", "unreachable", "gone"]
ReadyResult = Literal["started", "running", "missing", "no-teams", "started-already",
                      "unreachable", "gone"]
CubeResult = Literal["moved", "started", "unreachable", "cube-import", "failed"]


def _refusal_text(e: HostedRefused) -> str:
    body = e.body
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return str(body["error"])
    return f"the drafting server refused ({e.status})"


async def hosted_stage(draft_id: str) -> str | None:
    """The session's stage; "lost" when the server does not know it, None when unreachable."""
    try:
        return (await hosted_client().status(draft_id)).stage
    except HostedUnavailable as e:
        logger.warning(f"Hosted status for DB{draft_id} unavailable: {e}")
        return None
    except HostedRefused as e:
        if e.status == 404:
            return "lost"
        logger.error(f"Hosted status for DB{draft_id} refused: {e}")
        return None


async def pause_for_player(session_id: str) -> PauseResult:
    """A player's /pause. It holds until /unpause; the poller never lifts it."""
    async with _draft_lock(session_id):
        row = await DraftSession.get_by_session_id(session_id)
        if row is None:
            return "gone"
        if row.hosted_paused_by == HUMAN_PAUSE:
            return "already"
        try:
            await hosted_client().pause(row.draft_id)
        except HostedUnavailable as e:
            logger.warning(f"Could not pause hosted draft {session_id}: {e}")
            return "unreachable"
        except HostedRefused as e:
            logger.info(f"Hosted pause of {session_id} refused: {e}")
            return "not-drafting"
        # Over a pause for an empty seat too: a player asked for this one.
        await _store(session_id, hosted_paused_by=HUMAN_PAUSE)
        logger.info(f"Hosted draft {session_id} paused by a player")
        return "paused"


async def resume_for_players(session_id: str) -> ResumeResult:
    """/unpause, once its ready check has passed."""
    async with _draft_lock(session_id):
        row = await DraftSession.get_by_session_id(session_id)
        if row is None:
            return "gone"
        try:
            await hosted_client().resume(row.draft_id)
        except HostedUnavailable as e:
            logger.warning(f"Could not resume hosted draft {session_id}: {e}")
            return "unreachable"
        except HostedRefused as e:
            logger.info(f"Hosted resume of {session_id} refused: {e}")
            await _store(session_id, hosted_paused_by=None, hosted_pause_notice_id=None)
            return "not-drafting"
        await _store(session_id, hosted_paused_by=None, hosted_pause_notice_id=None)
        tracked = _TRACKED.get(session_id)
        if tracked is not None:
            tracked.paused_for = None
        logger.info(f"Hosted draft {session_id} resumed by the players")
        return "resumed"


async def stop_hosted_draft(session_id: str) -> StopResult:
    """/scrap: stop the session for good. A stopped draft has no log and is never polled again."""
    async with _draft_lock(session_id):
        row = await DraftSession.get_by_session_id(session_id)
        if row is None:
            return "gone"
        try:
            await hosted_client().stop(row.draft_id)
        except HostedUnavailable as e:
            logger.warning(f"Could not stop hosted draft {session_id}: {e}")
            return "unreachable"
        except HostedRefused as e:
            if e.status != 404:
                logger.info(f"Hosted stop of {session_id} refused: {e}")
                return "ended"
        _forget(session_id)
        _UNPULLABLE.add(session_id)
        logger.info(f"Stopped hosted draft {session_id}")
        return "stopped"


async def stop_if_hosted(session_id: str, draft_id: str | None, route: str | None) -> None:
    """Close a hosted draft's session as its draft goes away: cancelled, reaped, abandoned.

    Best effort, and never raises: a session the server no longer has (404) or
    has already ended or stopped (409) is what was wanted, and one it cannot
    be asked about is logged -- the user's action goes ahead either way, and
    the server closes a never-started session on its own after a while.
    Pass the row's values rather than the row: callers read them before the
    row is deleted.
    """
    if route != ROUTE or not draft_id:
        return
    try:
        async with _draft_lock(session_id):
            try:
                await hosted_client().stop(draft_id)
            except HostedRefused as e:
                if e.status not in (404, 409):
                    raise
            _forget(session_id)
            _UNPULLABLE.add(session_id)
        logger.info(f"Closed the hosted session DB{draft_id} of {session_id}")
    except Exception as e:
        logger.warning(f"Could not close the hosted session DB{draft_id} of {session_id}: {e}")


async def release_hosted_logs(bot: discord.Client, session_id: str) -> bool:
    """/release_draft_logs: unlock the log on the server, then post it in Discord.

    A failed unlock still posts: the captured log is the deliverable, and the
    server unlocks it on its own timer anyway.
    """
    row = await DraftSession.get_by_session_id(session_id)
    if row is None:
        return False
    draft_id = row.draft_id

    async def release() -> None:
        await unlock_hosted_log(session_id, draft_id)

    return await publish_draft_log(bot, session_id, release=True, release_log=release)


async def unlock_hosted_log(session_id: str, draft_id: str) -> None:
    """Unlock a hosted draft's log for its players. Best effort; never raises.

    For /release_draft_logs and for the clinch, which unlocks the log early:
    the server unlocks it on its own timer anyway.
    """
    try:
        await hosted_client().release_log(draft_id)
    except Exception as e:
        logger.warning(f"Could not unlock the hosted log of {session_id}: {e}")


async def replace_seat_with_bot(session_id: str, discord_id: str) -> tuple[ReplaceResult, str]:
    """An admin's /replace_seat: a bot takes over that player's seat and pool.

    Returns the outcome and the seat name -- or, when refused, the server's reason.
    """
    async with _draft_lock(session_id):
        row = await DraftSession.get_by_session_id(session_id)
        if row is None:
            return "gone", ""
        seat = seat_names(row.sign_ups or {}).get(discord_id)
        if seat is None:
            return "not-in-draft", ""
        try:
            await hosted_client().replace_with_bot(row.draft_id, seat)
        except HostedUnavailable as e:
            logger.warning(f"Could not replace seat {seat!r} of {session_id}: {e}")
            return "unreachable", seat
        except HostedRefused as e:
            logger.info(f"Replacing seat {seat!r} of {session_id} refused: {e}")
            return "refused", _refusal_text(e)
        bots = list(row.hosted_bot_seats or [])
        if seat not in bots:
            bots.append(seat)
        await _store(session_id, hosted_bot_seats=bots)
        logger.info(f"Seat {seat!r} of hosted draft {session_id} is now played by a bot")
        return "replaced", seat


async def request_ready_check(bot: discord.Client, session_id: str) -> tuple[ReadyResult, list[str]]:
    """/ready: re-send the seating and run a new in-app ready check now.

    The poller runs one on its own once every seat is connected; this is for
    when it is waiting on something nobody can fix from Draftmancer -- such as
    a "not ready" answered after a check had already timed out. Returns the
    outcome and, when seats are empty, whose.
    """
    async with _draft_lock(session_id):
        row = await DraftSession.get_by_session_id(session_id)
        if row is None:
            return "gone", []
        if not row.session_stage:
            return "no-teams", []
        try:
            status = await hosted_client().status(row.draft_id)
        except (HostedUnavailable, HostedRefused) as e:
            logger.warning(f"/ready for {session_id}: no hosted status: {e}")
            return "unreachable", []
        if status.stage != "gathering":
            return "started-already", []
        away = [s.name for s in status.seats if not s.connected]
        if away:
            return "missing", away
        st = _TRACKED.setdefault(session_id, _Tracked())
        if st.ready_check is not None:
            return "running", []
        channel = bot.get_channel(int(row.draft_channel_id)) if row.draft_channel_id else None
        if channel is None:
            return "gone", []
        try:
            await hosted_client().set_seating(row.draft_id, seat_list(row.sign_ups or {}))
            st.retry = None
            await _open_ready_check(bot, row, len(status.seats), st, channel)
        except (HostedUnavailable, HostedRefused) as e:
            logger.warning(f"/ready for {session_id} failed: {e}")
            return "unreachable", []
        return "started", []


MutinyResult = Literal["moved", "moved-unstopped", "ended", "refused", "gone"]


async def mutiny_hosted_draft(session_id: str, channel: Any,
                              requested_by: str) -> tuple[MutinyResult, str]:
    """/mutiny on a hosted draft: close the hosted session, then move to the fallback.

    The session is stopped first, so nobody keeps drafting there and no
    end-of-draft push arrives later. A server that cannot be reached moves the
    draft anyway -- an outage is what /mutiny is for -- and says the old room
    may come back ("moved-unstopped"); its late push finds no draft, since the
    draft id changes. A draft that has already ended there is left alone: its
    log is on the way. Returns the outcome and, when refused, the server's reason.
    """
    async with _draft_lock(session_id):
        row = await DraftSession.get_by_session_id(session_id)
        if row is None or row.draftmancer_route != ROUTE:
            return "gone", ""
        stopped = True
        try:
            await hosted_client().stop(row.draft_id)
        except HostedUnavailable as e:
            logger.warning(f"/mutiny could not stop hosted draft {session_id}; moving anyway: {e}")
            stopped = False
        except HostedRefused as e:
            if e.status == 409:
                return "ended", ""
            if e.status != 404:
                logger.error(f"/mutiny: stopping hosted draft {session_id} refused: {e}")
                return "refused", _refusal_text(e)
        _forget(session_id)
        _UNPULLABLE.add(session_id)
        reason: FallbackReason = "mutiny" if stopped else "mutiny-unstopped"
        await open_fallback_draft(row, reason, channel=channel, requested_by=requested_by)
        return ("moved" if stopped else "moved-unstopped"), ""


def _new_draft_id() -> str:
    """Same shape as SessionDetails' draft ids."""
    return "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(8))


async def change_hosted_cube(bot: discord.Client, session_id: str, cube: str,
                             packs: int, cards: int) -> CubeResult:
    """Update Cube on a hosted queue: move the queue to a new session with the new cube.

    The server takes a session's cube and pack settings only at creation, so
    the draft gets a new draft_id -- and a new link -- as the manager's
    regenerate_draft_session does. The new session is made before the old one
    is stopped, so a failure leaves the queue where it was. Refused once the
    draft has started.
    """
    async with _draft_lock(session_id):
        row = await DraftSession.get_by_session_id(session_id)
        if row is None:
            return "failed"
        old_id = row.draft_id
        stage = await hosted_stage(old_id)
        if stage is None:
            return "unreachable"
        if stage not in ("gathering", "lost"):
            return "started"
        new_id = _new_draft_id()
        seats = seat_list(row.sign_ups or {})
        spec = build_session_spec(
            draft_id=new_id, players=seats, cube_id=cube, packs_per_player=packs,
            cards_per_pack=cards, pick_timer=DRAFT_PICK_TIMER_SECONDS,
            unlock_timer_minutes=DRAFT_LOG_UNLOCK_TIMER_MINUTES, log_webhook=log_webhook_url())
        client = hosted_client()
        try:
            await client.create(spec)
        except HostedCubeImportFailed as e:
            logger.warning(f"The hosted server could not import cube {cube} for {session_id}: {e}")
            return "cube-import"
        except HostedUnavailable as e:
            logger.warning(f"Could not open a new hosted session for {session_id}: {e}")
            return "unreachable"
        except HostedRefused as e:
            logger.error(f"New hosted session for {session_id} refused: {e}")
            return "failed"
        # Compare-and-swap: only the queue that was checked moves.
        async with db_session() as session:
            result = await session.execute(
                update(DraftSession)
                .where(DraftSession.session_id == session_id, DraftSession.draft_id == old_id)
                .values(draft_id=new_id, draft_link=get_draftmancer_session_url(new_id),
                        cube=cube, packs_per_player=packs, cards_per_pack=cards))
        if getattr(result, "rowcount", 0) == 0:
            logger.warning(f"{session_id} changed while its cube was being updated; keeping it")
            await _stop_quietly(client, new_id)
            return "failed"
        if stage != "lost":
            await _stop_quietly(client, old_id)
        logger.info(f"Moved hosted queue {session_id} from DB{old_id} to DB{new_id} for cube {cube}")

        channel = bot.get_channel(int(row.draft_channel_id)) if row.draft_channel_id else None
        tracked = _TRACKED.pop(session_id, None)
        if tracked is not None and tracked.ready_check is not None:
            await _end_ready_check(channel, tracked, None)
        _DONE.discard(session_id)
        if row.session_stage:
            try:
                await client.set_seating(new_id, seats)
            except (HostedUnavailable, HostedRefused) as e:
                logger.warning(f"Could not seat the new hosted session of {session_id}: {e}")
        if row.teams_start_time is not None and channel is not None:
            await _announce_move(session_id, channel)
        return "moved"


async def _stop_quietly(client: HostedDraftmancer, draft_id: str) -> None:
    try:
        await client.stop(draft_id)
    except (HostedUnavailable, HostedRefused) as e:
        logger.warning(f"Could not stop hosted session DB{draft_id}: {e}")


async def _announce_move(session_id: str, channel: Any) -> None:
    """Every player's new link: the links handed out with the teams now lead nowhere."""
    row = await DraftSession.get_by_session_id(session_id)
    if row is None:
        return
    links = {discord_id: link for discord_id, name in (row.sign_ups or {}).items()
             if (link := row.get_draft_link_for_user(name, user_id=discord_id))}
    lines = "\n".join(f"<@{discord_id}> — [your new draft link]({link})"
                      for discord_id, link in links.items())
    try:
        await channel.send(
            f"⚠️ **The cube changed, so the draft moved to a new Draftmancer room** "
            f"(`DB{row.draft_id}`). **Any link you were given before this message no "
            f"longer works.**\n\n{lines}",
            allowed_mentions=_PING_USERS_ONLY)
    except Exception as e:
        logger.error(f"Could not announce the moved session of {session_id}: {e}")


# --- the end ------------------------------------------------------------------------------

async def on_draft_ended(bot: discord.Client, session_id: str, log: DraftLog) -> None:
    """Finish the draft: rooms and pairings at most once, then the log.

    The push can arrive twice and race the reconciler's pull; finish_draft's
    claim keeps rooms to one build and capture skips a captured log. Calls for
    one draft are also run one after another, so the second finds the first's
    work done rather than racing it.
    """
    lock = _END_LOCKS.setdefault(session_id, asyncio.Lock())
    async with lock:
        await finish_draft(bot, session_id, log)
    _forget(session_id)


async def accept_pushed_log(bot: discord.Client, wire_session_id: str, log: DraftLog) -> bool:
    """The webhook's handler: find the draft and finish it in the background.

    False when no hosted draft has this session id. Answers before the work is
    done: building rooms takes longer than the server waits for a reply, and a
    timed-out push would only be sent again.
    """
    draft_id = wire_session_id.removeprefix("DB")
    if not draft_id or draft_id == wire_session_id:
        return False
    async with db_session() as session:
        session_id = await session.scalar(select(DraftSession.session_id).where(
            DraftSession.draft_id == draft_id, DraftSession.draftmancer_route == ROUTE))
    if session_id is None:
        return False
    if session_id not in _FINISHING or _FINISHING[session_id].done():
        task = asyncio.create_task(_finish_logged(bot, session_id, log))
        _FINISHING[session_id] = task
        task.add_done_callback(lambda t, sid=session_id: _drop_finishing(sid, t))
    return True


def _drop_finishing(session_id: str, task: asyncio.Task[None]) -> None:
    if _FINISHING.get(session_id) is task:
        del _FINISHING[session_id]


async def _finish_logged(bot: discord.Client, session_id: str, log: DraftLog) -> None:
    try:
        await on_draft_ended(bot, session_id, log)
    except Exception:
        logger.opt(exception=True).error(f"Finishing pushed hosted draft {session_id} failed")


async def wait_for_finishing() -> None:
    """Wait for every pushed log being finished (tests, shutdown)."""
    tasks = list(_FINISHING.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# --- the reconciler's pull -------------------------------------------------------------

async def pull_ended_draft(bot: discord.Client, session_id: str, draft_id: str) -> None:
    """The reconciler's safety net for a lost push: fetch an ended draft's log and finish it.

    Asks status first, because a log 404 means both "not ended yet" and "no
    such session" -- and a session the server lost (a restart without its
    state) would otherwise be asked about until the reconciler's window ran
    out. A stopped or lost draft is never asked about again; an unreachable
    server is tried again next tick. Never raises for API trouble.
    """
    if session_id in _UNPULLABLE:
        return
    client = hosted_client()
    try:
        status = await client.status(draft_id)
        if status.stage == "stopped":
            logger.info(f"[reconciler] hosted draft {session_id} was stopped; it has no log")
            _UNPULLABLE.add(session_id)
            return
        if status.stage != "ended":
            return
        log = await client.fetch_log(draft_id)
    except HostedUnavailable as e:
        logger.warning(f"[reconciler] hosted API unreachable for {session_id}; retrying next tick: {e}")
        return
    except HostedLogStopped:
        logger.info(f"[reconciler] hosted draft {session_id} was stopped; it has no log")
        _UNPULLABLE.add(session_id)
        return
    except HostedRefused as e:
        if e.status == 404:
            logger.error(f"[reconciler] the hosted server no longer knows {session_id} "
                         f"(DB{draft_id}); its log is lost")
            _UNPULLABLE.add(session_id)
        else:
            logger.error(f"[reconciler] hosted API refused for {session_id}: {e}")
        return
    if log is None:
        return
    logger.info(f"[reconciler] pulled the hosted log of {session_id}")
    await on_draft_ended(bot, session_id, log)


# --- the fallback's log -----------------------------------------------------------------

async def pull_fallback_log(bot: discord.Client, session_id: str, draft_id: str) -> None:
    """The reconciler's fetch of a fallback draft's log from draftmancer.com.

    draftmancer.com gives the log out only once the draft has ended, and only
    when the players' room shares it with everyone (or has unlocked a delayed
    one); until then -- or for good, if it never does -- there is nothing to
    take, and the reconciler asks again until its window ends. Never raises
    for draftmancer.com's trouble.
    """
    log = await fetch_public_log(public_draftmancer_url(), draft_id)
    if log is None:
        logger.debug(f"[reconciler] no public log yet for fallback draft {session_id}")
        return
    logger.info(f"[reconciler] fetched the draftmancer.com log of fallback draft {session_id}")
    await on_draft_ended(bot, session_id, log)


# --- health ------------------------------------------------------------------------------
#
# Every request the client makes reports what it says about the server. Failing
# without a break for OUTAGE_ALERT_AFTER_SECONDS is an outage: the admins hear
# once (by DM, to the same SERVE_ALERT_DISCORD_ID the custodian's health monitor
# alerts), each hosted draft's channel -- gathering or drafting -- hears once,
# and both hear when it is back. A single answer ends the outage, so a flapping
# server is never one.
#
# A server refusing our token (401/403) is up but unusable -- every new draft
# falls back -- and no restart fixes it: the admins hear at once, once per
# incident, and again when an authenticated request is accepted.

OUTAGE_ALERT_AFTER_SECONDS = 180
ALERT_ENV = "SERVE_ALERT_DISCORD_ID"

OUTAGE_NOTICE = (
    "⚠️ **The drafting server isn't answering.** DraftBot keeps retrying, and the "
    "draft carries on from where it was once it's back. If it stays down, any player "
    "in this draft can move it to draftmancer.com with `/mutiny`."
)
RECOVERED_NOTICE = "✅ The drafting server is answering again."


@dataclass
class _Health:
    failing_since: float | None = None    # The first failure with no answer since.
    declared: bool = False                # This outage has lasted long enough to report.
    admins_told: bool = False
    recovered_after: float | None = None  # How long a reported outage lasted, until that is said.
    channels_told: dict[str, Any] = field(default_factory=dict)  # session_id -> channel
    unauthorized: bool = False            # The server is refusing our token.
    auth_told: bool = False
    auth_recovered: bool = False          # A reported refusal has ended, until that is said.


_HEALTH = _Health()


def _observe_api(outcome: Outcome) -> None:
    """The client's report on one request."""
    h = _HEALTH
    if outcome == "unanswered":
        if h.failing_since is None:
            h.failing_since = _now()
        return
    if h.failing_since is not None and h.declared:
        h.recovered_after = _now() - h.failing_since
    h.failing_since = None
    h.declared = False
    h.admins_told = False
    if outcome == "unauthorized":
        h.unauthorized = True
    elif outcome == "answered" and h.unauthorized:
        h.auth_recovered = h.auth_told
        h.unauthorized = False
        h.auth_told = False


def _known_down() -> bool:
    since = _HEALTH.failing_since
    return since is not None and _now() - since >= OUTAGE_ALERT_AFTER_SECONDS


async def _tell_admins(bot: discord.Client, text: str) -> bool:
    """True once the admins have been told -- or there is nobody to tell."""
    to = os.getenv(ALERT_ENV, "").strip()
    if not to:
        logger.error(f"Hosted Draftmancer: {text!r} -- {ALERT_ENV} is not set, so nobody is told")
        return True
    return await send_dm(bot, to, text, label="hosted Draftmancer health")


async def _post(channel: Any, text: str) -> bool:
    try:
        await channel.send(text)
        return True
    except Exception as e:
        logger.warning(f"Could not post a hosted health notice: {e}")
        return False


async def _report_health(bot: discord.Client, rows: list[Any]) -> None:
    """Once per tick: say what has changed about the server's health. Never raises."""
    try:
        await _report_auth(bot)
        await _report_outage(bot, rows)
    except Exception:
        logger.opt(exception=True).error("Reporting the hosted Draftmancer's health failed")


async def _report_auth(bot: discord.Client) -> None:
    h = _HEALTH
    if h.auth_recovered:
        if await _tell_admins(bot, "✅ The hosted Draftmancer accepts DraftBot's token again."):
            h.auth_recovered = False
    if h.unauthorized and not h.auth_told:
        logger.error("The hosted Draftmancer is refusing DraftBot's API token")
        h.auth_told = await _tell_admins(bot, (
            f"⚠️ **The hosted Draftmancer is refusing DraftBot's API token** "
            f"({draftmancer_api_url()}). It is up, but every new draft falls back to "
            f"draftmancer.com until DRAFTMANCER_API_TOKEN matches the server's "
            f"DRAFTBOT_API_TOKEN."))


async def _report_outage(bot: discord.Client, rows: list[Any]) -> None:
    h = _HEALTH
    if h.recovered_after is not None:
        for channel in h.channels_told.values():
            await _post(channel, RECOVERED_NOTICE)
        h.channels_told.clear()
        minutes = max(1, round(h.recovered_after / 60))
        if await _tell_admins(bot, f"✅ The hosted Draftmancer is answering again "
                                   f"(down about {minutes} minutes)."):
            h.recovered_after = None
    since = h.failing_since
    if since is None or not _known_down():
        return
    if not h.declared:
        h.declared = True
        logger.error(f"The hosted Draftmancer has not answered for {OUTAGE_ALERT_AFTER_SECONDS} s")
    if not h.admins_told:
        minutes = int((_now() - since) // 60)
        h.admins_told = await _tell_admins(bot, (
            f"⚠️ **The hosted Draftmancer hasn't answered for {minutes} minutes** "
            f"({draftmancer_api_url()}). Drafts on it wait where they were, and new "
            f"drafts open on draftmancer.com instead. If it stays down, `/mutiny` in a "
            f"draft's channel -- by any of its players or an admin -- moves that draft "
            f"to draftmancer.com."))
    # Gathering queues too: their players are waiting on the server as well.
    for row in rows:
        if row.session_id in h.channels_told or not row.draft_channel_id:
            continue
        channel: Any = bot.get_channel(int(row.draft_channel_id))
        if channel is not None and await _post(channel, OUTAGE_NOTICE):
            h.channels_told[row.session_id] = channel
