"""Player-side find-a-team actions.

Every function takes the caller's session and returns data; none commits or
sends a DM. The caller commits, then sends the returned notices.
"""

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.scheduler import PlayerAvailability
from models.team_finder import LfgRequest, LfgTeam, LfgTeamMember
from services.availability import (
    CELL_MINUTES,
    UsualWeek,
    cells_from_bytes,
    cells_to_bytes,
    naive_utc,
    window_cells,
)

TEAM_SIZES = (3, 4)
NOTE_MAX = 200
_FORMAT = "draftbot.scheduler.teams"
_VERSION = 1

# Every team-finder write path (the API) holds this across check -> service call -> commit, so checks and inserts never interleave in this one process.
WRITE_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class Notice:
    """A DM to send after commit; link_view is set when it carries a fresh link."""

    user_id: str
    text: str
    link_view: str | None = None


@dataclass
class ActionResult:
    ok: bool
    code: str | None = None
    notices: list[Notice] = field(default_factory=list[Notice])


def _refuse(code: str) -> ActionResult:
    return ActionResult(ok=False, code=code)


def _floor_half_hour(moment: datetime) -> datetime:
    utc = moment.astimezone(timezone.utc)
    return utc.replace(minute=utc.minute - utc.minute % CELL_MINUTES, second=0, microsecond=0)


def overlap_hours(weeks: list[UsualWeek | None], start_utc: datetime) -> float:
    """Hours in the next 7 days in which every week is free; a None week is never free."""
    if not weeks:
        return 0.0
    free: frozenset[int] | None = None
    for week in weeks:
        if week is None:
            return 0.0
        cells = window_cells(week.zone, week.cells, start_utc)
        free = cells if free is None else free & cells
    return len(free or ()) * CELL_MINUTES / 60


def _hours_text(hours: float) -> str:
    rounded = round(hours * 2) / 2
    number = str(int(rounded)) if rounded == int(rounded) else f"{rounded:.1f}"
    return f"{number} hour" if rounded == 1 else f"{number} hours"


def _iso(moment: datetime) -> str:
    return moment.replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


async def _weeks(session: AsyncSession, users: list[str]) -> dict[str, UsualWeek]:
    if not users:
        return {}
    rows = (
        await session.execute(
            select(PlayerAvailability).where(PlayerAvailability.user_id.in_(users))
        )
    ).scalars()
    return {
        str(r.user_id): UsualWeek(zone=str(r.zone), cells=cells_from_bytes(bytes(r.cells)))
        for r in rows
    }


async def _current_members(session: AsyncSession, team_id: int) -> list[LfgTeamMember]:
    return list(
        (
            await session.execute(
                select(LfgTeamMember)
                .where(LfgTeamMember.team_id == team_id, LfgTeamMember.left_utc.is_(None))
                .order_by(LfgTeamMember.id)
            )
        ).scalars()
    )


def _team_id(raw: str) -> int | None:
    return int(raw) if raw.isascii() and raw.isdigit() else None


async def _pending(session: AsyncSession, team_id: int, user_id: str) -> LfgRequest | None:
    return (
        await session.execute(
            select(LfgRequest).where(
                LfgRequest.team_id == team_id,
                LfgRequest.user_id == user_id,
                LfgRequest.status == "pending",
            )
        )
    ).scalars().first()


async def open_team(
    session: AsyncSession,
    guild_id: str,
    user_id: str,
    name: str,
    size: int,
    note: str,
    now: datetime,
) -> ActionResult:
    if size not in TEAM_SIZES or len(note) > NOTE_MAX:
        return _refuse("bad_input")
    stamp = naive_utc(now)
    team = LfgTeam(
        guild_id=guild_id,
        captain_id=user_id,
        size=size,
        note=note,
        taking_requests=True,
        status="open",
        created_utc=stamp,
    )
    session.add(team)
    await session.flush()
    session.add(
        LfgTeamMember(team_id=team.id, user_id=user_id, display_name=name, joined_utc=stamp)
    )
    await session.flush()
    return ActionResult(ok=True)


async def request_join(
    session: AsyncSession,
    guild_id: str,
    user_id: str,
    name: str,
    team_id: str,
    note: str,
    now: datetime,
) -> ActionResult:
    if len(note) > NOTE_MAX:
        return _refuse("bad_input")
    tid = _team_id(team_id)
    team = await session.get(LfgTeam, tid) if tid is not None else None
    if team is None or team.guild_id != guild_id or team.status == "closed":
        return _refuse("team_closed")
    members = await _current_members(session, int(team.id))
    if any(m.user_id == user_id for m in members):
        return _refuse("already_member")
    if team.status == "full" or len(members) >= int(team.size):
        return _refuse("team_full")
    if not team.taking_requests:
        return _refuse("not_taking_requests")
    if await _pending(session, int(team.id), user_id) is not None:
        return ActionResult(ok=True)

    try:
        async with session.begin_nested():
            session.add(
                LfgRequest(
                    team_id=team.id,
                    user_id=user_id,
                    display_name=name,
                    note=note,
                    status="pending",
                    created_utc=naive_utc(now),
                )
            )
    except IntegrityError:
        return ActionResult(ok=True)

    weeks = await _weeks(session, [user_id] + [str(m.user_id) for m in members])
    hours = overlap_hours(
        [weeks.get(user_id)] + [weeks.get(str(m.user_id)) for m in members],
        _floor_half_hour(now),
    )
    captain = next((m for m in members if m.user_id == team.captain_id), None)
    captain_name = str(captain.display_name) if captain else str(team.captain_id)
    text = f"{name} asked to join {captain_name}'s team. You overlap {_hours_text(hours)} a week."
    return ActionResult(ok=True, notices=[Notice(str(team.captain_id), text, "lfg")])


async def withdraw_request(
    session: AsyncSession, user_id: str, team_id: str, now: datetime
) -> ActionResult:
    tid = _team_id(team_id)
    request = await _pending(session, tid, user_id) if tid is not None else None
    if request is None:
        return _refuse("request_gone")
    request.status = "withdrawn"
    request.answered_utc = naive_utc(now)
    await session.flush()
    return ActionResult(ok=True)


async def set_taking_requests(
    session: AsyncSession, user_id: str, team_id: str, on: bool, now: datetime
) -> ActionResult:
    tid = _team_id(team_id)
    team = await session.get(LfgTeam, tid) if tid is not None else None
    if team is None or team.captain_id != user_id:
        return _refuse("not_captain")
    team.taking_requests = on
    await session.flush()
    return ActionResult(ok=True)


def _cells_b64(week: UsualWeek | None) -> str | None:
    if week is None:
        return None
    return base64.b64encode(cells_to_bytes(week.cells)).decode("ascii")


async def teams_state(session: AsyncSession, guild_id: str, user_id: str) -> dict[str, object]:
    """The ``draftbot.scheduler.teams`` v1 body as seen by ``user_id``."""
    teams = list(
        (
            await session.execute(
                select(LfgTeam)
                .where(LfgTeam.guild_id == guild_id, LfgTeam.status.in_(("open", "full")))
                .order_by(LfgTeam.created_utc, LfgTeam.id)
            )
        ).scalars()
    )
    members = {int(t.id): await _current_members(session, int(t.id)) for t in teams}
    weeks = await _weeks(
        session,
        sorted({str(m.user_id) for ms in members.values() for m in ms}),
    )

    out_teams: list[dict[str, object]] = []
    on_teams: list[str] = []
    for team in teams:
        tid = int(team.id)
        ms = members[tid]
        if any(m.user_id == user_id for m in ms):
            on_teams.append(str(tid))
        captain = next((m for m in ms if m.user_id == team.captain_id), None)
        out_teams.append(
            {
                "team_id": str(tid),
                "captain": str(team.captain_id),
                "captain_name": str(captain.display_name) if captain else str(team.captain_id),
                "size": int(team.size),
                "note": str(team.note),
                "taking_requests": bool(team.taking_requests),
                "status": str(team.status),
                "members": [
                    {
                        "user": str(m.user_id),
                        "name": str(m.display_name),
                        "zone": w.zone if (w := weeks.get(str(m.user_id))) else None,
                        "cells": _cells_b64(weeks.get(str(m.user_id))),
                    }
                    for m in ms
                ],
            }
        )

    outgoing_rows = (
        await session.execute(
            select(LfgRequest)
            .where(LfgRequest.user_id == user_id)
            .order_by(LfgRequest.created_utc, LfgRequest.id)
        )
    ).scalars()
    listed = {int(t.id) for t in teams}
    latest: dict[int, LfgRequest] = {}
    for r in outgoing_rows:  # ordered oldest first, so the last row per team wins
        if int(r.team_id) in listed:
            latest[int(r.team_id)] = r
    outgoing = [
        {"team_id": str(r.team_id), "status": str(r.status), "note": str(r.note)}
        for r in latest.values()
    ]

    captained = [int(t.id) for t in teams if t.captain_id == user_id]
    incoming: dict[str, list[dict[str, object]]] = {}
    if captained:
        pending = list(
            (
                await session.execute(
                    select(LfgRequest)
                    .where(LfgRequest.team_id.in_(captained), LfgRequest.status == "pending")
                    .order_by(LfgRequest.created_utc, LfgRequest.id)
                )
            ).scalars()
        )
        req_weeks = await _weeks(session, sorted({str(r.user_id) for r in pending}))
        for tid in captained:
            incoming[str(tid)] = [
                {
                    "user": str(r.user_id),
                    "name": str(r.display_name),
                    "note": str(r.note),
                    "zone": w.zone if (w := req_weeks.get(str(r.user_id))) else None,
                    "cells": _cells_b64(req_weeks.get(str(r.user_id))),
                    "requested_at": _iso(r.created_utc),
                }
                for r in pending
                if int(r.team_id) == tid
            ]

    return {
        "format": _FORMAT,
        "version": _VERSION,
        "teams": out_teams,
        "me": {
            "user": user_id,
            "on_teams": on_teams,
            "outgoing": outgoing,
            "incoming": incoming,
        },
    }
