from datetime import datetime, timezone

import pytest

from database.db_session import db_session
from models.scheduler import PlayerAvailability
from services.availability import UsualWeek, cells_to_bytes
from services.team_finder import (
    Notice,
    open_team,
    overlap_hours,
    request_join,
    set_taking_requests,
    teams_state,
    withdraw_request,
)

NOW = datetime(2026, 10, 22, 12, 10, 0, tzinfo=timezone.utc)
G = "g1"
ALL = frozenset(range(336))


def _week(cells=ALL, zone="UTC"):
    return UsualWeek(zone=zone, cells=frozenset(cells))


async def _save_week(session, user, cells=ALL, zone="UTC"):
    session.add(PlayerAvailability(
        user_id=user, zone=zone, cells=cells_to_bytes(cells), version=1,
        stored_utc=datetime(2026, 10, 1)))
    await session.flush()


async def _open(session, user="cap", name="Rook", size=3, note=""):
    r = await open_team(session, G, user, name, size, note, NOW)
    assert r.ok, r.code
    await session.flush()
    state = await teams_state(session, G, user)
    return state["teams"][-1]["team_id"]


def test_overlap_hours():
    assert overlap_hours([_week(), _week(range(0, 24))], NOW) == 12.0
    assert overlap_hours([_week(), None], NOW) == 0.0
    assert overlap_hours([], NOW) == 0.0


@pytest.mark.asyncio
async def test_open_team_validation(test_db):
    async with db_session() as s:
        for size in (2, 5):
            assert (await open_team(s, G, "c", "C", size, "", NOW)).code == "bad_input"
        assert (await open_team(s, G, "c", "C", 3, "x" * 201, NOW)).code == "bad_input"
        assert (await open_team(s, G, "c", "C", 3, "x" * 200, NOW)).ok


@pytest.mark.asyncio
async def test_round_trip_and_notice_text(test_db):
    async with db_session() as s:
        await _save_week(s, "cap", range(0, 24))
        await _save_week(s, "ben")
        tid = await _open(s, "cap", "Rook", 3, "late nights")
        r = await request_join(s, G, "ben", "Ben", tid, "hi", NOW)
        assert r.ok and r.code is None
        assert r.notices == [Notice("cap", "Ben asked to join Rook's team. You overlap 12 hours a week.", "lfg")]
        await s.flush()
        st = await teams_state(s, G, "cap")
        (team,) = st["teams"]
        assert team["captain"] == "cap" and team["captain_name"] == "Rook"
        assert team["note"] == "late nights" and team["taking_requests"] is True
        assert team["status"] == "open" and team["size"] == 3
        assert team["members"][0]["user"] == "cap" and team["members"][0]["zone"] == "UTC"
        assert st["me"]["on_teams"] == [tid]
        inc = st["me"]["incoming"][tid]
        assert inc[0]["user"] == "ben" and inc[0]["name"] == "Ben" and inc[0]["note"] == "hi"
        assert inc[0]["requested_at"] == "2026-10-22T12:10:00Z"
        assert inc[0]["zone"] == "UTC" and isinstance(inc[0]["cells"], str)
        sb = await teams_state(s, G, "ben")
        assert sb["me"]["outgoing"] == [{"team_id": tid, "status": "pending", "note": "hi"}]
        assert sb["me"]["incoming"] == {} and sb["me"]["on_teams"] == []
        assert st["format"] == "draftbot.scheduler.teams" and st["version"] == 1


@pytest.mark.asyncio
async def test_notice_hours_format(test_db):
    async with db_session() as s:
        await _save_week(s, "cap", range(0, 3))
        await _save_week(s, "ben")
        tid = await _open(s)
        r = await request_join(s, G, "ben", "Ben", tid, "", NOW)
        assert "overlap 1.5 hours a week" in r.notices[0].text
        await _save_week(s, "cap2", range(0, 2))
        tid2 = await _open(s, "cap2", "Two")
        r = await request_join(s, G, "ben", "Ben", tid2, "", NOW)
        assert "overlap 1 hour a week" in r.notices[0].text


@pytest.mark.asyncio
async def test_member_without_week_state(test_db):
    async with db_session() as s:
        await _open(s)
        st = await teams_state(s, G, "x")
        m = st["teams"][0]["members"][0]
        assert m["zone"] is None and m["cells"] is None


@pytest.mark.asyncio
async def test_refusals(test_db):
    async with db_session() as s:
        tid = await _open(s)
        assert (await request_join(s, G, "cap", "Rook", tid, "", NOW)).code == "already_member"
        assert (await request_join(s, G, "b", "B", "999", "", NOW)).code == "team_closed"
        assert (await request_join(s, G, "b", "B", tid, "x" * 201, NOW)).code == "bad_input"
        assert (await set_taking_requests(s, "b", tid, False, NOW)).code == "not_captain"
        assert (await set_taking_requests(s, "cap", tid, False, NOW)).ok
        assert (await request_join(s, G, "b", "B", tid, "", NOW)).code == "not_taking_requests"
        assert (await set_taking_requests(s, "cap", tid, True, NOW)).ok
        assert (await withdraw_request(s, "b", tid, NOW)).code == "request_gone"


@pytest.mark.asyncio
async def test_duplicate_pending_and_rerequest(test_db):
    async with db_session() as s:
        tid = await _open(s)
        assert len((await request_join(s, G, "b", "B", tid, "one", NOW)).notices) == 1
        await s.flush()
        r = await request_join(s, G, "b", "B", tid, "two", NOW)
        assert r.ok and r.notices == []
        await s.flush()
        st = await teams_state(s, G, "b")
        assert st["me"]["outgoing"] == [{"team_id": tid, "status": "pending", "note": "one"}]
        assert (await withdraw_request(s, "b", tid, NOW)).ok
        await s.flush()
        assert (await teams_state(s, G, "b"))["me"]["outgoing"][0]["status"] == "withdrawn"
        r = await request_join(s, G, "b", "B", tid, "again", NOW)
        assert r.ok and len(r.notices) == 1
        await s.flush()
        out = (await teams_state(s, G, "b"))["me"]["outgoing"]
        assert [o["status"] for o in out].count("pending") == 1


@pytest.mark.asyncio
async def test_team_full_and_closed_not_listed(test_db):
    from models.team_finder import LfgTeam, LfgTeamMember
    from sqlalchemy import select
    async with db_session() as s:
        tid = await _open(s)
        team = (await s.execute(select(LfgTeam))).scalar_one()
        for u in ("m1", "m2"):
            s.add(LfgTeamMember(team_id=team.id, user_id=u, display_name=u, joined_utc=datetime(2026, 1, 1)))
        team.status = "full"
        await s.flush()
        assert (await request_join(s, G, "z", "Z", tid, "", NOW)).code == "team_full"
        assert len((await teams_state(s, G, "z"))["teams"]) == 1
        team.status = "closed"
        await s.flush()
        assert (await request_join(s, G, "z", "Z", tid, "", NOW)).code == "team_closed"
        assert (await teams_state(s, G, "z"))["teams"] == []


@pytest.mark.asyncio
async def test_multiple_teams_and_ordering_and_guild(test_db):
    async with db_session() as s:
        a = await _open(s, "cap", "Rook")
        b = await _open(s, "cap", "Rook")
        await open_team(s, "other", "o", "O", 3, "", NOW)
        await s.flush()
        st = await teams_state(s, G, "cap")
        assert [t["team_id"] for t in st["teams"]] == [a, b]
        assert st["me"]["on_teams"] == [a, b]
