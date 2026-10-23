"""Player-side find-a-team actions.

Every function takes the caller's session and returns data; none commits or
sends a DM. The caller commits, then sends the returned notices.
"""

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.scheduler import PlayerAvailability
from models.team_finder import LfgRequest, LfgTeam, LfgTeamMember
from models.tournament import TournamentParticipant, TournamentTeamMember
from services.availability import (
    CELL_MINUTES,
    UsualWeek,
    cells_from_bytes,
    cells_to_bytes,
    iso_z,
    naive_utc,
    window_cells,
)
from services.tournament_service import get_active_tournament

TEAM_SIZES = (3, 4)
NOTE_MAX = 200
MAX_CAPTAINED = 2  # open or full teams one player may captain in a guild
REQUEST_DM_GAP = timedelta(minutes=10)  # no second captain DM for the same player and team within this
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


async def _guild_team(session: AsyncSession, guild_id: str, team_id: str) -> LfgTeam | None:
    """The team, or None when it does not exist or belongs to another guild."""
    tid = _team_id(team_id)
    team = await session.get(LfgTeam, tid) if tid is not None else None
    if team is None or team.guild_id != guild_id:
        return None
    return team


async def _captain_team(
    session: AsyncSession, guild_id: str, user_id: str, team_id: str
) -> LfgTeam | ActionResult:
    """The caller's own live team in this guild, or the refusal to return instead."""
    team = await _guild_team(session, guild_id, team_id)
    if team is None:
        return _refuse("team_closed")
    if team.captain_id != user_id:
        return _refuse("not_captain")
    if team.status == "closed":
        return _refuse("team_closed")
    return team


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


async def _pending_requests(session: AsyncSession, team_id: int) -> list[LfgRequest]:
    return list(
        (
            await session.execute(
                select(LfgRequest)
                .where(LfgRequest.team_id == team_id, LfgRequest.status == "pending")
                .order_by(LfgRequest.id)
            )
        ).scalars()
    )


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
    captained = (
        await session.execute(
            select(LfgTeam.id).where(
                LfgTeam.guild_id == guild_id,
                LfgTeam.captain_id == user_id,
                LfgTeam.status.in_(("open", "full")),
            )
        )
    ).all()
    if len(captained) >= MAX_CAPTAINED:
        return _refuse("too_many_teams")
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
    team = await _guild_team(session, guild_id, team_id)
    if team is None or team.status == "closed":
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
    stamp = naive_utc(now)
    recent = (
        await session.execute(
            select(LfgRequest.id).where(
                LfgRequest.team_id == team.id,
                LfgRequest.user_id == user_id,
                LfgRequest.created_utc > stamp - REQUEST_DM_GAP,
            )
        )
    ).first()

    try:
        async with session.begin_nested():
            session.add(
                LfgRequest(
                    team_id=team.id,
                    user_id=user_id,
                    display_name=name,
                    note=note,
                    status="pending",
                    created_utc=stamp,
                )
            )
    except IntegrityError:
        return ActionResult(ok=True)
    if recent is not None:
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
    session: AsyncSession, guild_id: str, user_id: str, team_id: str, now: datetime
) -> ActionResult:
    team = await _guild_team(session, guild_id, team_id)
    if team is None:
        return _refuse("team_closed")
    request = await _pending(session, int(team.id), user_id)
    if request is None:
        return _refuse("request_gone")
    request.status = "withdrawn"
    request.answered_utc = naive_utc(now)
    await session.flush()
    return ActionResult(ok=True)


async def set_taking_requests(
    session: AsyncSession, guild_id: str, user_id: str, team_id: str, on: bool, now: datetime
) -> ActionResult:
    team = await _captain_team(session, guild_id, user_id, team_id)
    if not isinstance(team, LfgTeam):
        return team
    team.taking_requests = on
    await session.flush()
    return ActionResult(ok=True)


MULTI_TEAM_WARNING = "Being on more than one team needs a mod's permission. DM a mod first."


def _name_of(members: list[LfgTeamMember], user_id: str) -> str:
    member = next((m for m in members if m.user_id == user_id), None)
    return str(member.display_name) if member else user_id


async def _own_team(session: AsyncSession, user_id: str, team_id: str) -> LfgTeam | None:
    tid = _team_id(team_id)
    team = await session.get(LfgTeam, tid) if tid is not None else None
    if team is None or team.captain_id != user_id:
        return None
    return team


async def _live_memberships(session: AsyncSession, user_id: str, guild_id: str) -> int:
    """Current memberships in one guild on teams that are still open or full."""
    rows = (
        await session.execute(
            select(LfgTeamMember.id)
            .join(LfgTeam, LfgTeam.id == LfgTeamMember.team_id)
            .where(
                LfgTeamMember.user_id == user_id,
                LfgTeamMember.left_utc.is_(None),
                LfgTeam.guild_id == guild_id,
                LfgTeam.status.in_(("open", "full")),
            )
        )
    ).all()
    return len(rows)


async def accept(
    session: AsyncSession, captain_id: str, team_id: str, requester: str, now: datetime
) -> ActionResult:
    team = await _own_team(session, captain_id, team_id)
    if team is None:
        return _refuse("not_captain")
    if team.status == "closed":
        return _refuse("team_closed")
    members = await _current_members(session, int(team.id))
    if team.status == "full" or len(members) >= int(team.size):
        return _refuse("team_full")
    request = await _pending(session, int(team.id), requester)
    if request is None:
        return _refuse("request_gone")

    stamp = naive_utc(now)
    request.status = "accepted"
    request.answered_utc = stamp
    session.add(
        LfgTeamMember(
            team_id=team.id, user_id=requester, display_name=request.display_name, joined_utc=stamp
        )
    )
    await session.flush()

    captain_name = _name_of(members, captain_id)
    text = f"You're on {captain_name}'s team."
    if await _live_memberships(session, requester, str(team.guild_id)) > 1:
        text += f"\n{MULTI_TEAM_WARNING}"
    notices = [Notice(requester, text, "lfg")]

    if len(members) + 1 >= int(team.size):
        team.status = "full"
        team.taking_requests = False
        for other in await _pending_requests(session, int(team.id)):
            other.status = "filled"
            other.answered_utc = stamp
            notices.append(Notice(str(other.user_id), f"{captain_name}'s team filled up."))
        names = ", ".join([str(m.display_name) for m in members] + [str(request.display_name)])
        notices.append(
            Notice(
                captain_id,
                f"Your team is full: {names}. Register it with `/tournament register`.",
                "lfg",
            )
        )
    await session.flush()
    return ActionResult(ok=True, notices=notices)


async def decline(
    session: AsyncSession, captain_id: str, team_id: str, requester: str, now: datetime
) -> ActionResult:
    team = await _own_team(session, captain_id, team_id)
    if team is None:
        return _refuse("not_captain")
    request = await _pending(session, int(team.id), requester)
    if request is None:
        return _refuse("request_gone")
    request.status = "declined"
    request.answered_utc = naive_utc(now)
    await session.flush()
    captain_name = _name_of(await _current_members(session, int(team.id)), captain_id)
    return ActionResult(
        ok=True, notices=[Notice(requester, f"{captain_name}'s team declined your request.")]
    )


async def close_team(
    session: AsyncSession, captain_id: str, team_id: str, now: datetime
) -> ActionResult:
    team = await _own_team(session, captain_id, team_id)
    if team is None:
        return _refuse("not_captain")
    if team.status == "closed":
        return _refuse("team_closed")
    stamp = naive_utc(now)
    members = await _current_members(session, int(team.id))
    pending = await _pending_requests(session, int(team.id))
    team.status = "closed"
    team.closed_utc = stamp
    team.taking_requests = False
    for request in pending:
        request.status = "declined"
        request.answered_utc = stamp
    await session.flush()
    text = f"{_name_of(members, captain_id)} closed their team."
    readers = [str(m.user_id) for m in members if m.user_id != captain_id]
    readers += [str(r.user_id) for r in pending]
    return ActionResult(ok=True, notices=[Notice(uid, text) for uid in readers])


async def leave_team(
    session: AsyncSession, user_id: str, team_id: str, now: datetime
) -> ActionResult:
    tid = _team_id(team_id)
    team = await session.get(LfgTeam, tid) if tid is not None else None
    if team is None:
        return _refuse("bad_input")
    if team.captain_id == user_id:
        return await close_team(session, user_id, team_id, now)
    if team.status == "closed":
        return _refuse("team_closed")
    members = await _current_members(session, int(team.id))
    me = next((m for m in members if m.user_id == user_id), None)
    if me is None:
        return _refuse("bad_input")
    me.left_utc = naive_utc(now)
    if team.status == "full":
        team.status = "open"
        team.taking_requests = True
    await session.flush()
    return ActionResult(
        ok=True,
        notices=[Notice(str(team.captain_id), f"{me.display_name} left your team.")],
    )


async def reconcile_registered(session: AsyncSession, guild_id: str, now: datetime) -> list[Notice]:
    """Close or leave forming teams for players already registered in the active tournament.

    Idempotent. The caller holds WRITE_LOCK, commits, then sends the notices.
    """
    tournament = await get_active_tournament(session, guild_id)
    if tournament is None or tournament.status != "registration":
        return []
    participants = (
        await session.execute(
            select(TournamentParticipant).where(TournamentParticipant.tournament_id == tournament.id)
        )
    ).scalars().all()
    registered = {str(p.captain_user_id) for p in participants}
    if participants:
        registered |= {
            str(uid)
            for uid in (
                await session.execute(
                    select(TournamentTeamMember.user_id).where(
                        TournamentTeamMember.participant_id.in_([p.id for p in participants])
                    )
                )
            ).scalars()
        }
    if not registered:
        return []

    stamp = naive_utc(now)
    notices: list[Notice] = []
    teams = (
        await session.execute(
            select(LfgTeam).where(LfgTeam.guild_id == guild_id, LfgTeam.status.in_(("open", "full")))
        )
    ).scalars().all()
    for team in teams:
        members = await _current_members(session, int(team.id))
        if str(team.captain_id) in registered:
            for request in await _pending_requests(session, int(team.id)):
                request.status = "withdrawn"
                request.answered_utc = stamp
            team.status = "closed"
            team.closed_utc = stamp
            team.taking_requests = False
            text = f"{_name_of(members, str(team.captain_id))} registered their team for the tournament."
            notices += [
                Notice(str(m.user_id), text) for m in members if m.user_id != team.captain_id
            ]
            continue
        for member in members:
            if str(member.user_id) not in registered:
                continue
            member.left_utc = stamp
            if team.status == "full":
                team.status = "open"
                team.taking_requests = True
            notices.append(
                Notice(
                    str(team.captain_id),
                    f"{member.display_name} registered with another team.",
                )
            )

    pending = (
        await session.execute(
            select(LfgRequest)
            .join(LfgTeam, LfgTeam.id == LfgRequest.team_id)
            .where(
                LfgTeam.guild_id == guild_id,
                LfgRequest.status == "pending",
                LfgRequest.user_id.in_(registered),
            )
        )
    ).scalars().all()
    for request in pending:
        request.status = "withdrawn"
        request.answered_utc = stamp
    await session.flush()
    return notices


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
                    "requested_at": iso_z(r.created_utc),
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
