"""The team-draft JSON API: one matchup's sides and booking, and idempotent book/clear.

A ``team_draft`` link names its matchup in the link's subject. Every action is
announced in the matchup's thread once it is committed; an announcement that
fails is logged and never fails the booking.
"""
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aiohttp import web
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from database.db_session import db_session
from helpers.matchup import Roster, matchup_state
from models.team_draft import TeamDraft, TeamDraftAction
from services import team_draft
from services.availability import CELL_MINUTES, cells_b64, floor_half_hour, iso_z, naive_utc, usual_weeks
from services.booking import WINDOW_CELLS, BookingChange
from services.scheduler_http import (
    ANNOUNCE,
    DISPLAY_NAME,
    NOW,
    SIDE_PEOPLE,
    error,
    identity,
    read_body,
    view_refused,
)
from services.scheduler_links import LinkIdentity
from services.team_draft import DRAFT_CELLS, MIN_PER_SIDE

MAX_ACTION_BYTES = 1024
VIEW = "team_draft"
FORMAT = "draftbot.scheduler.team_draft"
_VERSION = 1
_ID_DIGITS = 18  # a subject longer than this is no matchup id we issued

routes = web.RouteTableDef()


class BadInput(Exception):
    pass


def subject_id(who: LinkIdentity) -> int | None:
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


def booking_doc(start: datetime | None, by_name: str | None, version: int) -> dict[str, object]:
    return {
        "start_utc": None if start is None else iso_z(start),
        "by_name": None if by_name is None else str(by_name),
        "version": int(version),
    }


async def build_state(
    request: web.Request,
    who: LinkIdentity,
    matchup: dict[str, object],
    booking: dict[str, object],
    rosters: tuple[Roster, Roster],
) -> dict[str, object]:
    """The ``draftbot.scheduler.team_draft`` v1 body for one matchup, whatever owns it."""
    now: datetime = request.app[NOW]()
    # Rosters are read outside any session: resolving them may reach Discord.
    async with db_session() as session:
        weeks = await usual_weeks(
            session, sorted({user for roster in rosters for user, _name in roster.people})
        )
    return {
        "format": FORMAT,
        "version": _VERSION,
        "matchup": matchup,
        "window": {
            "start_utc": iso_z(floor_half_hour(now)),
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


async def _state(request: web.Request, team_draft_id: int) -> dict[str, object] | None:
    """The ``draftbot.scheduler.team_draft`` v1 body, or None if the matchup is gone."""
    who = identity(request)
    async with db_session() as session:
        row = await _matchup(session, team_draft_id, who.guild_id)
        if row is None:
            return None
        thread_url = (
            f"https://discord.com/channels/{who.guild_id}/{row.thread_id}" if row.thread_id else None
        )
        matchup: dict[str, object] = {
            "id": int(row.id),
            "name": str(row.name),
            "thread_url": thread_url,
            "state": matchup_state(row.draft_session_id is not None),
        }
        booking = booking_doc(row.booked_start_utc, row.booked_by_name, row.booking_version)

    # Resolved from the guild's cache on every load, so someone who joins a
    # side's role later shows up; read outside the session above.
    rosters = await request.app[SIDE_PEOPLE](who.guild_id, team_draft_id)
    return await build_state(request, who, matchup, booking, rosters)


async def serve_state(request: web.Request, link: "LinkSubject") -> web.Response:
    """GET for a link view: its subject's state, or 404 once the subject is gone."""
    if (refused := view_refused(request, link.view)) is not None:
        return refused
    subject = subject_id(identity(request))
    state = None if subject is None else await link.state(request, subject)
    if state is None:
        return error(404, "gone")
    return web.json_response(state)


@routes.get("/api/team-draft")
async def get_team_draft(request: web.Request) -> web.Response:
    return await serve_state(request, TEAM_DRAFT)


def start_utc_of(body: dict[str, Any]) -> datetime:
    """``start_utc`` as an aware UTC moment; only ISO 8601 with a trailing Z is accepted."""
    value = body.get("start_utc")
    if not isinstance(value, str) or not value.endswith("Z"):
        raise BadInput
    moment = datetime.fromisoformat(value[:-1])  # ValueError -> bad_input
    if moment.tzinfo is not None:
        raise BadInput
    return moment.replace(tzinfo=timezone.utc)


def parse_action(body: object) -> tuple[str, str, datetime | None, int]:
    """(action id, kind, start or None for clear, seen version) from a request body."""
    if not isinstance(body, dict):
        raise BadInput
    action_id = body.get("action_id")
    if not isinstance(action_id, str):
        raise BadInput
    action_id = str(uuid.UUID(action_id))
    seen_version = body.get("seen_version")
    if isinstance(seen_version, bool) or not isinstance(seen_version, int):
        raise BadInput
    kind = body.get("kind")
    if kind == "book":
        return action_id, kind, start_utc_of(body), seen_version
    if kind == "clear":
        return action_id, kind, None, seen_version
    raise BadInput


@dataclass(frozen=True)
class LinkSubject:
    """What a link view books, and how. Both views share one action ledger,
    told apart by ``key``: an action id recorded for one never replays as the other."""
    view: str
    key: str  # the subject id's name in an action's stored result
    label: str  # how the log names one subject
    exists: Callable[[AsyncSession, str, int], Awaitable[bool]]  # (session, guild_id, subject)
    book: Callable[[AsyncSession, int, str, str, datetime, int, datetime], Awaitable[BookingChange]]
    clear: Callable[[AsyncSession, int, str, str, int, datetime], Awaitable[BookingChange]]
    state: Callable[[web.Request, int], Awaitable[dict[str, object] | None]]
    announce: web.AppKey[Callable[[int, BookingChange], Awaitable[None]]]


async def _reply(request: web.Request, link: LinkSubject, subject: int, ok: bool, code: str | None) -> web.Response:
    state = await link.state(request, subject)
    if state is None:
        return error(404, "gone")
    return web.json_response({"ok": ok, "code": code, "state": state})


async def serve_action(request: web.Request, link: LinkSubject) -> web.Response:
    """POST for a link view: book or clear its subject's time, once per action id."""
    if (refused := view_refused(request, link.view)) is not None:
        return refused
    raw = await read_body(request, MAX_ACTION_BYTES)
    if raw is None:
        return error(413, "too_large")
    try:
        action_id, kind, start_utc, seen_version = parse_action(json.loads(raw))
    except (ValueError, BadInput):  # json, uuid and isoformat errors are ValueErrors
        return error(400, "bad_input")

    who = identity(request)
    subject = subject_id(who)
    if subject is None:
        return error(404, "gone")
    async with db_session() as session:
        if not await link.exists(session, who.guild_id, subject):
            return error(404, "gone")
    now: datetime = request.app[NOW]()
    name = await request.app[DISPLAY_NAME](who.guild_id, who.user_id)

    # Only the database work holds the lock; the reply and the announcement
    # may wait on Discord, and every other booking would wait with them.
    async with team_draft.WRITE_LOCK:
        async with db_session() as session:
            seen = await session.get(TeamDraftAction, action_id)
            if seen is not None:
                stored = json.loads(str(seen.result_json))
                if seen.user_id != who.user_id or stored.get(link.key) != subject:
                    return error(400, "bad_input")
                change, result = None, (bool(stored["ok"]), stored["code"])
            else:
                change = (
                    await link.book(session, subject, who.user_id, name, start_utc, seen_version, now)
                    if start_utc is not None
                    else await link.clear(session, subject, who.user_id, name, seen_version, now)
                )
                result = change.ok, change.code
                session.add(
                    TeamDraftAction(
                        action_id=action_id,
                        user_id=who.user_id,
                        result_json=json.dumps({"ok": change.ok, "code": change.code, link.key: subject}),
                        done_utc=naive_utc(now),
                    )
                )
                await session.commit()

    if change is not None and change.ok:
        try:
            await request.app[link.announce](subject, change)
        except Exception:  # an announcement must never fail the committed booking
            logger.exception(f"{link.label} {subject}: announcing the {kind} raised")
    return await _reply(request, link, subject, *result)


async def _team_draft_exists(session: AsyncSession, guild_id: str, team_draft_id: int) -> bool:
    return await _matchup(session, team_draft_id, guild_id) is not None


TEAM_DRAFT = LinkSubject(
    view=VIEW,
    key="team_draft_id",
    label="Team draft",
    exists=_team_draft_exists,
    book=team_draft.book,
    clear=team_draft.clear,
    state=_state,
    announce=ANNOUNCE,
)


@routes.post("/api/team-draft/actions")
async def post_action(request: web.Request) -> web.Response:
    return await serve_action(request, TEAM_DRAFT)
