"""The team-draft JSON API: one matchup's sides and booking, and idempotent book/clear.

A ``team_draft`` link names its matchup in the link's subject. Every action is
announced in the matchup's thread once it is committed; an announcement that
fails is logged and never fails the booking.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from aiohttp import web
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from database.db_session import db_session
from helpers.matchup import matchup_state
from models.team_draft import TeamDraft, TeamDraftAction
from services import team_draft
from services.availability import CELL_MINUTES, cells_b64, iso_z, naive_utc, usual_weeks
from services.scheduler_http import (
    ANNOUNCE,
    DISPLAY_NAME,
    NOW,
    SIDE_PEOPLE,
    error,
    identity,
    read_body,
)
from services.scheduler_links import LinkIdentity
from services.team_draft import (
    DRAFT_CELLS,
    MIN_PER_SIDE,
    WINDOW_CELLS,
    BookingChange,
    window_start,
)

MAX_ACTION_BYTES = 1024
VIEW = "team_draft"
FORMAT = "draftbot.scheduler.team_draft"
_VERSION = 1
_ID_DIGITS = 18  # a subject longer than this is no matchup id we issued

routes = web.RouteTableDef()


class _BadInput(Exception):
    pass


def _view_refused(request: web.Request) -> web.Response | None:
    if identity(request).view != VIEW:
        return error(403, "wrong_view")
    return None


def _subject_id(who: LinkIdentity) -> int | None:
    subject = who.subject
    if subject is None or not subject.isascii() or not subject.isdigit() or len(subject) > _ID_DIGITS:
        return None
    return int(subject)


async def _matchup(session: AsyncSession, team_draft_id: int, guild_id: str) -> TeamDraft | None:
    """The link's matchup, but only if it belongs to the link's guild."""
    row = await session.get(TeamDraft, team_draft_id)
    if row is None or str(row.guild_id) != guild_id:
        return None
    return row


async def _state(request: web.Request, team_draft_id: int) -> dict[str, object] | None:
    """The ``draftbot.scheduler.team_draft`` v1 body, or None if the matchup is gone."""
    who = identity(request)
    now: datetime = request.app[NOW]()
    async with db_session() as session:
        row = await _matchup(session, team_draft_id, who.guild_id)
        if row is None:
            return None
        thread_url = (
            f"https://discord.com/channels/{who.guild_id}/{row.thread_id}" if row.thread_id else None
        )
        matchup = {
            "id": int(row.id),
            "name": str(row.name),
            "thread_url": thread_url,
            "state": matchup_state(row.draft_session_id is not None),
        }
        booking = {
            "start_utc": None if row.booked_start_utc is None else iso_z(row.booked_start_utc),
            "by_name": None if row.booked_by_name is None else str(row.booked_by_name),
            "version": int(row.booking_version),
        }

    # Resolved from the guild's cache on every load, so someone who joins a
    # side's role later shows up; read outside the session above.
    rosters = await request.app[SIDE_PEOPLE](who.guild_id, team_draft_id)
    async with db_session() as session:
        weeks = await usual_weeks(
            session, sorted({user for roster in rosters for user, _name in roster.people})
        )
    return {
        "format": FORMAT,
        "version": _VERSION,
        "matchup": matchup,
        "window": {
            "start_utc": iso_z(window_start(now)),
            "cell_minutes": CELL_MINUTES,
            "cells": WINDOW_CELLS,
        },
        "rules": {"min_per_side": MIN_PER_SIDE, "draft_cells": DRAFT_CELLS},
        "sides": [
            {
                "label": roster.label,
                "people": [
                    {
                        "user": user,
                        "name": name,
                        "zone": w.zone if (w := weeks.get(user)) else None,
                        "cells": cells_b64(weeks.get(user)),
                    }
                    for user, name in roster.people
                ],
            }
            for roster in rosters
        ],
        "booking": booking,
        "me": {"user": who.user_id},
    }


@routes.get("/api/team-draft")
async def get_team_draft(request: web.Request) -> web.Response:
    if (refused := _view_refused(request)) is not None:
        return refused
    team_draft_id = _subject_id(identity(request))
    state = None if team_draft_id is None else await _state(request, team_draft_id)
    if state is None:
        return error(404, "gone")
    return web.json_response(state)


def _start_utc(body: dict[str, Any]) -> datetime:
    """``start_utc`` as an aware UTC moment; only ISO 8601 with a trailing Z is accepted."""
    value = body.get("start_utc")
    if not isinstance(value, str) or not value.endswith("Z"):
        raise _BadInput
    moment = datetime.fromisoformat(value[:-1])  # ValueError -> bad_input
    if moment.tzinfo is not None:
        raise _BadInput
    return moment.replace(tzinfo=timezone.utc)


def _parse(body: object) -> tuple[str, str, datetime | None, int]:
    """(action id, kind, start or None for clear, seen version) from a request body."""
    if not isinstance(body, dict):
        raise _BadInput
    action_id = body.get("action_id")
    if not isinstance(action_id, str):
        raise _BadInput
    action_id = str(uuid.UUID(action_id))
    seen_version = body.get("seen_version")
    if isinstance(seen_version, bool) or not isinstance(seen_version, int):
        raise _BadInput
    kind = body.get("kind")
    if kind == "book":
        return action_id, kind, _start_utc(body), seen_version
    if kind == "clear":
        return action_id, kind, None, seen_version
    raise _BadInput


async def _reply(request: web.Request, team_draft_id: int, ok: bool, code: str | None) -> web.Response:
    state = await _state(request, team_draft_id)
    if state is None:
        return error(404, "gone")
    return web.json_response({"ok": ok, "code": code, "state": state})


@routes.post("/api/team-draft/actions")
async def post_action(request: web.Request) -> web.Response:
    if (refused := _view_refused(request)) is not None:
        return refused
    raw = await read_body(request, MAX_ACTION_BYTES)
    if raw is None:
        return error(413, "too_large")
    try:
        action_id, kind, start_utc, seen_version = _parse(json.loads(raw))
    except (ValueError, _BadInput):  # json, uuid and isoformat errors are ValueErrors
        return error(400, "bad_input")

    who = identity(request)
    team_draft_id = _subject_id(who)
    if team_draft_id is None:
        return error(404, "gone")
    async with db_session() as session:
        if await _matchup(session, team_draft_id, who.guild_id) is None:
            return error(404, "gone")
    now: datetime = request.app[NOW]()
    name = await request.app[DISPLAY_NAME](who.guild_id, who.user_id)

    async with team_draft.WRITE_LOCK:
        async with db_session() as session:
            seen = await session.get(TeamDraftAction, action_id)
            if seen is not None:
                stored = json.loads(str(seen.result_json))
                if seen.user_id != who.user_id or stored.get("team_draft_id") != team_draft_id:
                    return error(400, "bad_input")
                return await _reply(request, team_draft_id, bool(stored["ok"]), stored["code"])
            change: BookingChange = (
                await team_draft.book(
                    session, team_draft_id, who.user_id, name, start_utc, seen_version, now
                )
                if start_utc is not None
                else await team_draft.clear(session, team_draft_id, who.user_id, name, seen_version, now)
            )
            session.add(
                TeamDraftAction(
                    action_id=action_id,
                    user_id=who.user_id,
                    result_json=json.dumps(
                        {"ok": change.ok, "code": change.code, "team_draft_id": team_draft_id}
                    ),
                    done_utc=naive_utc(now),
                )
            )
            await session.commit()

    if change.ok:
        try:
            await request.app[ANNOUNCE](team_draft_id, change)
        except Exception:  # an announcement must never fail the committed booking
            logger.exception(f"Team draft {team_draft_id}: announcing the {kind} raised")
    return await _reply(request, team_draft_id, change.ok, change.code)
