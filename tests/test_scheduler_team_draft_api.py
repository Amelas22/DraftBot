"""The team-draft JSON API on draftbot.fyi: one matchup's sides, booking and actions."""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from database.db_session import db_session
from helpers.matchup import Roster
from models.team_draft import TeamDraft
from services.availability import UsualWeek, save_usual_week
from services.scheduler_links import issue_link
from services.scheduler_web import build_app
from services.team_draft import Picks, create_team_draft

NOW = datetime(2026, 10, 26, 12, 10, tzinfo=timezone.utc)
START = "2026-10-28T19:30:00Z"
GUILD = "222"
ROSTERS = (
    Roster(label="Team Garruk", people=(("101", "Ann"),)),
    Roster(label="Ash, Bo", people=(("201", "Ash"), ("202", "Bo"))),
)


async def seed(guild=GUILD, thread_id="900"):
    async with db_session() as session:
        row = await create_team_draft(
            session, guild, "50", "Team Garruk vs Ash, Bo",
            Picks(role_ids=("111",), user_ids=()),
            Picks(role_ids=(), user_ids=("201", "202")),
            created_by="300", now=NOW,
        )
        row.thread_id = thread_id
        return row.id


def make_app(announce=None, side_people=None):
    async def display_name(guild_id, user_id):
        return "name" + user_id

    async def rosters(guild_id, team_draft_id):
        return ROSTERS

    return build_app(
        now=lambda: NOW,
        display_name=display_name,
        side_people=side_people or rosters,
        announce=announce or AsyncMock(),
        enabled=lambda g: True,
    )


@pytest_asyncio.fixture
async def announce(test_db):
    return AsyncMock()


@pytest_asyncio.fixture
async def client(announce):
    async with TestClient(TestServer(make_app(announce=announce))) as c:
        yield c


async def _auth(team_draft_id, user="111", view="team_draft", guild=GUILD):
    url = await issue_link(user, guild, view, NOW, subject=str(team_draft_id))
    return {"Authorization": "Bearer " + url.split("#")[1]}


async def _act(client, headers, **body):
    body.setdefault("action_id", str(uuid.uuid4()))
    return await client.post("/api/team-draft/actions", data=json.dumps(body), headers=headers)


@pytest.mark.asyncio
async def test_state_lists_both_sides_people_and_the_booking(client):
    team_draft_id = await seed()
    await save_usual_week("201", UsualWeek(zone="Europe/London", cells=frozenset({0, 1})), NOW)
    async with db_session() as session:
        row = await session.get(TeamDraft, team_draft_id)
        row.booked_start_utc = datetime(2026, 10, 28, 19, 30)
        row.booked_by_name = "Bo"
        row.booking_version = 3

    resp = await client.get("/api/team-draft", headers=await _auth(team_draft_id))

    assert resp.status == 200
    assert await resp.json() == {
        "format": "draftbot.scheduler.team_draft",
        "version": 1,
        "matchup": {
            "id": team_draft_id,
            "name": "Team Garruk vs Ash, Bo",
            "thread_url": f"https://discord.com/channels/{GUILD}/900",
            "state": "open",
        },
        "window": {"start_utc": "2026-10-26T12:00:00Z", "cell_minutes": 30, "cells": 336},
        "rules": {"min_per_side": 2, "draft_cells": 2},
        "sides": [
            {"label": "Team Garruk", "people": [
                {"user": "101", "name": "Ann", "zone": None, "cells": None}]},
            {"label": "Ash, Bo", "people": [
                {"user": "201", "name": "Ash", "zone": "Europe/London",
                 "cells": "wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"},
                {"user": "202", "name": "Bo", "zone": None, "cells": None}]},
        ],
        "booking": {"start_utc": START, "by_name": "Bo", "version": 3},
        "me": {"user": "111"},
    }


@pytest.mark.asyncio
async def test_a_started_matchup_without_a_thread_reads_started(client):
    team_draft_id = await seed(thread_id=None)
    async with db_session() as session:
        (await session.get(TeamDraft, team_draft_id)).draft_session_id = "s-1"

    body = await (await client.get("/api/team-draft", headers=await _auth(team_draft_id))).json()

    assert body["matchup"]["state"] == "started"
    assert body["matchup"]["thread_url"] is None
    assert body["booking"] == {"start_utc": None, "by_name": None, "version": 0}


@pytest.mark.asyncio
async def test_a_lfg_link_is_refused(client):
    team_draft_id = await seed()
    headers = await _auth(team_draft_id, view="lfg")

    resp = await client.get("/api/team-draft", headers=headers)
    assert resp.status == 403
    assert await resp.json() == {"error": "wrong_view"}
    resp = await _act(client, headers, kind="book", start_utc=START, seen_version=0)
    assert resp.status == 403
    assert await resp.json() == {"error": "wrong_view"}


@pytest.mark.asyncio
async def test_a_link_for_another_guilds_matchup_is_gone(client, announce):
    elsewhere = await seed(guild="other")
    no_subject = {"Authorization": "Bearer " + (
        await issue_link("111", GUILD, "team_draft", NOW)).split("#")[1]}

    for headers in (await _auth(elsewhere), await _auth(9999), no_subject):
        resp = await client.get("/api/team-draft", headers=headers)
        assert resp.status == 404
        assert await resp.json() == {"error": "gone"}
        resp = await _act(client, headers, kind="book", start_utc=START, seen_version=0)
        assert resp.status == 404
        assert await resp.json() == {"error": "gone"}
    async with db_session() as session:
        assert (await session.get(TeamDraft, elsewhere)).booked_start_utc is None
    announce.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_returns_new_state_and_announces_once(client, announce):
    team_draft_id = await seed()
    headers = await _auth(team_draft_id)
    action = str(uuid.uuid4())

    first = await _act(client, headers, action_id=action, kind="book", start_utc=START, seen_version=0)
    second = await _act(client, headers, action_id=action, kind="book", start_utc=START, seen_version=0)

    assert first.status == second.status == 200
    body = await first.json()
    assert body == await second.json()
    assert (body["ok"], body["code"]) == (True, None)
    assert body["state"]["booking"] == {"start_utc": START, "by_name": "name111", "version": 1}
    announce.assert_awaited_once()
    announced_id, change = announce.await_args.args
    assert announced_id == team_draft_id
    assert (change.ok, change.old, change.new, change.by_name) == (
        True, None, datetime(2026, 10, 28, 19, 30, tzinfo=timezone.utc), "name111")

    resp = await _act(client, headers, kind="clear", seen_version=1)
    body = await resp.json()
    assert (body["ok"], body["state"]["booking"]["start_utc"]) == (True, None)
    assert announce.await_count == 2


@pytest.mark.asyncio
async def test_a_replay_by_another_user_is_bad_input(client):
    team_draft_id = await seed()
    action = str(uuid.uuid4())
    await _act(client, await _auth(team_draft_id), action_id=action, kind="book",
               start_utc=START, seen_version=0)

    resp = await _act(client, await _auth(team_draft_id, user="999"), action_id=action,
                      kind="book", start_utc=START, seen_version=0)

    assert resp.status == 400
    assert await resp.json() == {"error": "bad_input"}


@pytest.mark.asyncio
async def test_a_refused_booking_does_not_announce(client, announce):
    team_draft_id = await seed()

    resp = await _act(client, await _auth(team_draft_id), kind="book", start_utc=START, seen_version=5)

    assert resp.status == 200
    body = await resp.json()
    assert (body["ok"], body["code"]) == (False, "changed")
    assert body["state"]["booking"]["version"] == 0
    announce.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_announce_failure_still_returns_ok(test_db):
    team_draft_id = await seed()
    boom = AsyncMock(side_effect=RuntimeError("discord down"))
    async with TestClient(TestServer(make_app(announce=boom))) as c:
        resp = await _act(c, await _auth(team_draft_id), kind="book", start_utc=START, seen_version=0)
        body = await resp.json()

    assert resp.status == 200
    assert body["ok"] is True
    assert body["state"]["booking"]["start_utc"] == START
    boom.assert_awaited_once()


@pytest.mark.asyncio
async def test_bad_bodies_are_bad_input(client, announce):
    team_draft_id = await seed()
    headers = await _auth(team_draft_id)
    action = str(uuid.uuid4())
    bodies = [
        b"not json",
        b"[]",
        json.dumps({"kind": "book", "start_utc": START, "seen_version": 0}).encode(),
        json.dumps({"action_id": "nope", "kind": "clear", "seen_version": 0}).encode(),
    ]
    for raw in bodies:
        resp = await client.post("/api/team-draft/actions", data=raw, headers=headers)
        assert resp.status == 400, raw
        assert await resp.json() == {"error": "bad_input"}
    for fields in (
        {"kind": "move", "start_utc": START, "seen_version": 0},
        {"kind": "book", "seen_version": 0},
        {"kind": "book", "start_utc": "2026-10-28T19:30:00", "seen_version": 0},
        {"kind": "book", "start_utc": "2026-10-28T19:30:00+00:00", "seen_version": 0},
        {"kind": "book", "start_utc": "2026-10-28T19:30:00+01:00Z", "seen_version": 0},
        {"kind": "book", "start_utc": "Thursday", "seen_version": 0},
        {"kind": "book", "start_utc": 1793318400, "seen_version": 0},
        {"kind": "book", "start_utc": START, "seen_version": True},
        {"kind": "book", "start_utc": START, "seen_version": "0"},
        {"kind": "book", "start_utc": START},
        {"kind": "clear"},
    ):
        resp = await _act(client, headers, action_id=action, **fields)
        assert resp.status == 400, fields
        assert await resp.json() == {"error": "bad_input"}
    resp = await client.post("/api/team-draft/actions", data=b"x" * 5000, headers=headers)
    assert resp.status == 413
    announce.assert_not_awaited()
    async with db_session() as session:
        assert (await session.get(TeamDraft, team_draft_id)).booking_version == 0
