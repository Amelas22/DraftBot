"""The tournament-match JSON API: one match's sides and booking, and idempotent book/clear.

A ``tournament_match`` link names its match in the link's subject, and speaks the
same document as a team draft's. Every committed action is announced in the
match's thread; an announcement that fails is logged and never fails the booking.
"""
import json
from datetime import datetime

from aiohttp import web
from loguru import logger

from database.db_session import db_session
from helpers.matchup import matchup_state
from models.team_draft import TeamDraftAction
from services import team_draft
from services.availability import naive_utc
from services.booking import BookingChange
from services.match_booking import MatchStore, book_match, clear_match, schedulable_match
from services.scheduler_http import (
    DISPLAY_NAME,
    MATCH_ANNOUNCE,
    MATCH_PEOPLE,
    NOW,
    error,
    identity,
    read_body,
    view_refused,
)
from services.scheduler_team_draft_api import (
    MAX_ACTION_BYTES,
    BadInput,
    booking_doc,
    build_state,
    parse_action,
    subject_id,
)

VIEW = "tournament_match"

routes = web.RouteTableDef()


async def _state(request: web.Request, match_id: int) -> dict[str, object] | None:
    """The ``draftbot.scheduler.team_draft`` v1 body for a match, or None if it has no two sides."""
    who = identity(request)
    # One read of the match's own row; the sides' names come from Discord after.
    async with db_session() as session:
        found = await schedulable_match(session, who.guild_id, match_id)
        current = await MatchStore(match_id).current(session)
        if found is None or current is None:
            return None
        row = found[0]
        thread_id, booked_by = row.thread_id, row.booked_by_name
    rosters = await request.app[MATCH_PEOPLE](who.guild_id, match_id)
    if rosters is None:
        return None
    start, version, started = current
    matchup: dict[str, object] = {
        "id": match_id,
        "name": f"{rosters[0].label} vs {rosters[1].label}",
        "thread_url": f"https://discord.com/channels/{who.guild_id}/{thread_id}" if thread_id else None,
        "state": matchup_state(started),
    }
    booking = booking_doc(start, booked_by, version)
    return await build_state(request, who, matchup, booking, rosters)


@routes.get("/api/tournament-match")
async def get_match(request: web.Request) -> web.Response:
    if (refused := view_refused(request, VIEW)) is not None:
        return refused
    match_id = subject_id(identity(request))
    state = None if match_id is None else await _state(request, match_id)
    if state is None:
        return error(404, "gone")
    return web.json_response(state)


async def _reply(request: web.Request, match_id: int, ok: bool, code: str | None) -> web.Response:
    state = await _state(request, match_id)
    if state is None:
        return error(404, "gone")
    return web.json_response({"ok": ok, "code": code, "state": state})


@routes.post("/api/tournament-match/actions")
async def post_action(request: web.Request) -> web.Response:
    if (refused := view_refused(request, VIEW)) is not None:
        return refused
    raw = await read_body(request, MAX_ACTION_BYTES)
    if raw is None:
        return error(413, "too_large")
    try:
        action_id, kind, start_utc, seen_version = parse_action(json.loads(raw))
    except (ValueError, BadInput):  # json, uuid and isoformat errors are ValueErrors
        return error(400, "bad_input")

    who = identity(request)
    match_id = subject_id(who)
    if match_id is None:
        return error(404, "gone")
    async with db_session() as session:
        if await schedulable_match(session, who.guild_id, match_id) is None:
            return error(404, "gone")
    now: datetime = request.app[NOW]()
    name = await request.app[DISPLAY_NAME](who.guild_id, who.user_id)

    async with team_draft.WRITE_LOCK:
        async with db_session() as session:
            seen = await session.get(TeamDraftAction, action_id)
            if seen is not None:
                stored = json.loads(str(seen.result_json))
                # A team-draft action has no match_id, so it never matches here.
                if seen.user_id != who.user_id or stored.get("match_id") != match_id:
                    return error(400, "bad_input")
                return await _reply(request, match_id, bool(stored["ok"]), stored["code"])
            change: BookingChange = (
                await book_match(session, match_id, who.user_id, name, start_utc, seen_version, now)
                if start_utc is not None
                else await clear_match(session, match_id, who.user_id, name, seen_version, now)
            )
            session.add(
                TeamDraftAction(
                    action_id=action_id,
                    user_id=who.user_id,
                    result_json=json.dumps({"ok": change.ok, "code": change.code, "match_id": match_id}),
                    done_utc=naive_utc(now),
                )
            )
            await session.commit()

    if change.ok:
        try:
            await request.app[MATCH_ANNOUNCE](match_id, change)
        except Exception:  # an announcement must never fail the committed booking
            logger.exception(f"Match {match_id}: announcing the {kind} raised")
    return await _reply(request, match_id, change.ok, change.code)
