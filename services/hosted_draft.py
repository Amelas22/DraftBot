"""Drafts run on the self-hosted Draftmancer, from queue to finished log.

DraftBot never sits in a hosted draft. It creates the session through the
control API when the queue opens, keeps the expected players and the seating in
step with Discord, and polls status to drive the draft channel: the status
message, Draftmancer's in-app ready check (after the Discord one has formed the
teams), the start, and a pause while a seat is empty mid-draft. The end arrives
as a pushed log -- or the reconciler's pull -- and runs finish_draft, which
builds rooms at most once.

Everything the poller acts on is re-read from the database and the status
endpoint every tick, so a restarted bot picks up where it was. What it keeps in
memory is only what the room has already been told (a ready check's messages, a
disconnect notice); losing that on a restart costs at most one repeated message.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import discord
from loguru import logger
from sqlalchemy import select, update

from config import (draftmancer_api_token, draftmancer_api_url, hosted_draftmancer_enabled,
                    scheduler_base_url)
from cube_views.pack_options import DEFAULT_CARDS_PER_PACK, DEFAULT_PACKS_PER_PLAYER
from database.db_session import db_session
from helpers.hosted_seats import discord_ids_by_seat, seat_list
from models.draft_session import DraftSession
from notification_service import send_ready_check_dms
from services.draft_end import DraftLog, finish_draft
from services.draft_messages import (
    READY_CHECK_PASSED,
    READY_CHECK_TIMEOUT_SECONDS,
    START_DELAY_SECONDS,
    format_status_message,
    ready_check_deadline_text,
    ready_check_left_text,
    ready_check_not_ready_text,
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
    HostedDraftmancer,
    HostedRefused,
    HostedSeat,
    HostedStatus,
    HostedUnavailable,
    build_session_spec,
)

ROUTE = "hosted"
POLL_INTERVAL_SECONDS = 5
# A player who answered "not ready" cannot answer the same check again (the
# in-app prompt is one-shot), so a fresh one is asked for after this pause.
NOT_READY_RETRY_SECONDS = 30
WEBHOOK_PATH = "/hosted/draft-ended"

# Only the mentions this module builds may ping: a display name is free text.
_PING_USERS_ONLY = discord.AllowedMentions(everyone=False, roles=False, users=True)


# --- the client -------------------------------------------------------------------

_client: HostedDraftmancer | None = None


def hosted_client() -> HostedDraftmancer:
    """The one client this process uses, made on first use from the environment."""
    global _client
    if _client is None:
        _client = HostedDraftmancer(draftmancer_api_url(), draftmancer_api_token())
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

async def open_hosted_draft(draft_session: Any) -> bool:
    """Create the draft's hosted session and record the route.

    False when it could not be created; the caller then runs the draft another
    way, and nothing here has been written.
    """
    if not hosted_draftmancer_enabled():
        return False
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
    except (HostedUnavailable, HostedRefused) as e:
        logger.error(f"Could not create the hosted session for {draft_session.session_id}: {e}")
        return False
    async with db_session() as session:
        await session.execute(update(DraftSession)
                              .where(DraftSession.session_id == draft_session.session_id)
                              .values(draftmancer_route=ROUTE))
    logger.info(f"Opened hosted session DB{draft_session.draft_id} for {draft_session.session_id}")
    return True


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
    paused_for: list[str] | None = None
    notice_id: str | None = None


_TRACKED: dict[str, _Tracked] = {}
# Drafts whose hosted session has ended, stopped or gone: no more polling.
_DONE: set[str] = set()
_END_LOCKS: dict[str, asyncio.Lock] = {}
_FINISHING: dict[str, asyncio.Task[None]] = {}


def forget_all() -> None:
    """Drop everything remembered (tests)."""
    _TRACKED.clear()
    _DONE.clear()


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
    async with db_session() as session:
        rows = list((await session.execute(
            select(DraftSession).where(
                DraftSession.draftmancer_route == ROUTE,
                DraftSession.rooms_created_at.is_(None),
                DraftSession.logs_captured_at.is_(None),
            ))).scalars())
    rows = [r for r in rows if r.session_id not in _DONE]
    results = await asyncio.gather(*(_poll_draft(bot, row) for row in rows),
                                   return_exceptions=True)
    for row, result in zip(rows, results):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            logger.opt(exception=result).error(f"Polling hosted draft {row.session_id} failed")


async def _poll_draft(bot: discord.Client, row: Any) -> None:
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
        raise
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
            await _end_ready_check(channel, st, None)
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


async def _start(row: Any, st: _Tracked, channel: Any) -> None:
    await _end_ready_check(channel, st, None)
    await channel.send(READY_CHECK_PASSED)
    await asyncio.sleep(START_DELAY_SECONDS)
    missing = await hosted_client().start(row.draft_id)
    if missing:
        # Someone dropped in the last seconds; a new check runs once they are back.
        logger.info(f"Hosted start of {row.session_id} refused; waiting on {missing}")
        await channel.send(ready_check_left_text(missing[0]))
        return
    logger.info(f"Hosted draft {row.session_id} started")


async def _drafting(row: Any, status: HostedStatus, st: _Tracked, channel: Any) -> None:
    st.ready_check = None
    away = _away_past_grace(status.seats)
    if away:
        if st.paused_for is None:
            await hosted_client().pause(row.draft_id)
            st.paused_for = away
            logger.info(f"Paused hosted draft {row.session_id}: {away} disconnected")
            if channel is not None:
                notice = await channel.send(_paused_text(row, away), allowed_mentions=_PING_USERS_ONLY)
                st.notice_id = str(notice.id)
        elif away != st.paused_for and channel is not None:
            st.paused_for = away
            await _edit_notice(channel, st, _paused_text(row, away))
        return
    if any(not s.connected for s in status.seats):
        return  # Only a blip so far, or still paused while someone's blip passes.
    if st.paused_for is not None:
        await hosted_client().resume(row.draft_id)
        logger.info(f"Resumed hosted draft {row.session_id}: every seat is back")
        if channel is not None:
            await _edit_notice(channel, st, _resumed_text(row, st.paused_for))
        st.paused_for = None
        st.notice_id = None


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
    who = ", ".join(_mentions(row, away))
    return f"▶️ **Draft resumed** — every seat is connected again ({who} dropped out for a while)."


async def _edit_notice(channel: Any, st: _Tracked, text: str) -> None:
    message = await update_or_send_message(channel, st.notice_id, content=text)
    if message is not None:
        st.notice_id = str(message.id)


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
