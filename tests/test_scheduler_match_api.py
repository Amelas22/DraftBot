"""The tournament-match JSON API on draftbot.fyi: a match's sides, booking and actions."""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import update

from conftest import seed_tournament_match, test_db  # noqa: F401
from database.db_session import db_session
from helpers.matchup import Roster
from match_control_view import match_side_rosters
from models.team_draft import TeamDraftAction
from models.tournament import Tournament, TournamentMatch
from services.availability import UsualWeek, save_usual_week
from services.scheduler_links import issue_link
from services.scheduler_web import build_app
from services.team_draft import Picks, create_team_draft

NOW = datetime(2026, 10, 26, 12, 10, tzinfo=timezone.utc)
START = "2026-10-28T19:30:00Z"
GUILD = "222"
ROSTERS = (
    Roster(label="Alpha", people=(("101", "Ann"),)),
    Roster(label="Bravo", people=(("201", "Ash"), ("202", "Bo"))),
)


async def seed(thread_id="900"):
    async with db_session() as s:
        match = await seed_tournament_match(s, thread_id=thread_id)
        await s.execute(update(Tournament).values(guild_id=GUILD))
        return match.id


def make_app(announce, people=None):
    async def display_name(guild_id, user_id):
        return "name" + user_id

    async def match_people(guild_id, match_id):
        return ROSTERS

    return build_app(
        now=lambda: NOW,
        display_name=display_name,
        match_people=people or match_people,
        match_announce=announce,
        enabled=lambda g: True,
    )


@pytest_asyncio.fixture
async def announce(test_db):  # noqa: F811
    return AsyncMock()


@pytest_asyncio.fixture
async def client(announce):
    async with TestClient(TestServer(make_app(announce))) as c:
        yield c


async def _auth(subject, user="101", view="tournament_match", guild=GUILD):
    url = await issue_link(user, guild, view, NOW, subject=str(subject))
    return {"Authorization": "Bearer " + url.split("#")[1]}


async def _act(client, headers, **body):
    body.setdefault("action_id", str(uuid.uuid4()))
    return await client.post("/api/tournament-match/actions", data=json.dumps(body), headers=headers)


@pytest.mark.asyncio
async def test_state_lists_both_sides_and_the_booking(client):
    match_id = await seed()
    await save_usual_week("201", UsualWeek(zone="Europe/London", cells=frozenset({0, 1})), NOW)
    async with db_session() as s:
        row = await s.get(TournamentMatch, match_id)
        row.booked_start_utc = datetime(2026, 10, 28, 19, 30)
        row.booked_by_name = "Bo"
        row.booking_version = 3

    resp = await client.get("/api/tournament-match", headers=await _auth(match_id))

    assert resp.status == 200
    body = await resp.json()
    assert body["format"] == "draftbot.scheduler.team_draft"
    assert body["matchup"]["id"] == match_id
    assert body["matchup"]["name"] == "Alpha vs Bravo" or body["matchup"]["name"] == "Bravo vs Alpha"
    assert body["matchup"]["thread_url"] == f"https://discord.com/channels/{GUILD}/900"
    assert body["matchup"]["state"] == "open"
    assert [side["label"] for side in body["sides"]] == ["Alpha", "Bravo"]
    assert [p["user"] for p in body["sides"][1]["people"]] == ["201", "202"]
    assert body["sides"][1]["people"][0]["zone"] == "Europe/London"
    assert body["rules"] == {"min_per_side": 2, "draft_cells": 2}
    assert body["booking"] == {"start_utc": START, "by_name": "Bo", "version": 3}
    assert body["me"] == {"user": "101"}


@pytest.mark.asyncio
async def test_the_name_follows_the_sides_as_listed(test_db):  # noqa: F811
    async def people(guild_id, match_id):
        return Roster("Zed", ()), Roster("Yan", ())

    async with TestClient(TestServer(make_app(AsyncMock(), people))) as c:
        match_id = await seed()
        body = await (await c.get("/api/tournament-match", headers=await _auth(match_id))).json()
    assert body["matchup"]["name"] == "Zed vs Yan"


@pytest.mark.asyncio
async def test_a_match_with_a_result_reads_started(client):
    match_id = await seed(thread_id=None)
    async with db_session() as s:
        row = await s.get(TournamentMatch, match_id)
        row.team_a_wins = 2
        row.team_b_wins = 0

    body = await (await client.get("/api/tournament-match", headers=await _auth(match_id))).json()

    assert body["matchup"]["state"] == "started"
    assert body["matchup"]["thread_url"] is None


@pytest.mark.asyncio
async def test_gone_for_a_bye_an_unknown_side_another_guild_and_an_unknown_id(test_db):  # noqa: F811
    bot = MagicMock()
    bot.get_guild = lambda gid: None
    bot.fetch_user = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=404), "gone"))

    async def people(guild_id, match_id):
        return await match_side_rosters(bot, guild_id, match_id)

    async with TestClient(TestServer(make_app(AsyncMock(), people))) as client:
        await _gone(client)


async def _gone(client):
    match_id = await seed()
    headers = await _auth(match_id)
    assert (await client.get("/api/tournament-match", headers=await _auth(match_id, guild="333"))).status == 404
    assert (await client.get("/api/tournament-match", headers=await _auth(99999))).status == 404
    async with db_session() as s:
        (await s.get(TournamentMatch, match_id)).team_b_participant_id = None
    resp = await client.get("/api/tournament-match", headers=headers)
    assert resp.status == 404
    assert await resp.json() == {"error": "gone"}
    resp = await _act(client, headers, kind="book", start_utc=START, seen_version=0)
    assert resp.status == 404
    async with db_session() as s:
        row = await s.get(TournamentMatch, match_id)
        row.is_bye = True
    assert (await client.get("/api/tournament-match", headers=headers)).status == 404


@pytest.mark.asyncio
async def test_a_team_draft_link_is_refused(client):
    match_id = await seed()
    headers = await _auth(match_id, view="team_draft")

    resp = await client.get("/api/tournament-match", headers=headers)
    assert resp.status == 403
    assert await resp.json() == {"error": "wrong_view"}
    resp = await _act(client, headers, kind="book", start_utc=START, seen_version=0)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_booking_saves_the_time_and_announces_once(client, announce):
    match_id = await seed()
    headers = await _auth(match_id)

    resp = await _act(client, headers, kind="book", start_utc=START, seen_version=0)

    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True and body["code"] is None
    assert body["state"]["booking"] == {"start_utc": START, "by_name": "name101", "version": 1}
    announce.assert_awaited_once()
    assert announce.await_args.args[0] == match_id
    assert announce.await_args.args[1].ok is True


@pytest.mark.asyncio
async def test_replaying_an_action_gives_the_same_reply_without_a_second_announce(client, announce):
    match_id = await seed()
    headers = await _auth(match_id)
    action_id = str(uuid.uuid4())
    first = await _act(client, headers, action_id=action_id, kind="book", start_utc=START, seen_version=0)
    again = await _act(client, headers, action_id=action_id, kind="book", start_utc=START, seen_version=0)

    assert again.status == 200
    assert (await again.json())["ok"] == (await first.json())["ok"]
    assert (await again.json())["state"]["booking"]["version"] == 1
    announce.assert_awaited_once()


@pytest.mark.asyncio
async def test_clearing_removes_the_time(client, announce):
    match_id = await seed()
    headers = await _auth(match_id)
    await _act(client, headers, kind="book", start_utc=START, seen_version=0)

    resp = await _act(client, headers, kind="clear", seen_version=1)

    body = await resp.json()
    assert body["ok"] is True
    assert body["state"]["booking"]["start_utc"] is None
    assert announce.await_count == 2


@pytest.mark.asyncio
async def test_a_stale_version_is_refused_and_not_announced(client, announce):
    match_id = await seed()
    headers = await _auth(match_id)
    await _act(client, headers, kind="book", start_utc=START, seen_version=0)

    body = await (await _act(client, headers, kind="book", start_utc=START, seen_version=0)).json()

    assert body["ok"] is False
    announce.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_team_draft_action_id_replayed_here_is_bad_input(client):
    match_id = await seed()
    action_id = str(uuid.uuid4())
    async with db_session() as s:
        draft = await create_team_draft(
            s, GUILD, "50", "A vs B", Picks(role_ids=("1",), user_ids=()),
            Picks(role_ids=(), user_ids=("2",)), created_by="300", now=NOW)
        s.add(TeamDraftAction(
            action_id=action_id, user_id="101",
            result_json=json.dumps({"ok": True, "code": None, "team_draft_id": draft.id}),
            done_utc=datetime(2026, 10, 26, 12, 10)))

    resp = await _act(client, await _auth(match_id), action_id=action_id,
                      kind="book", start_utc=START, seen_version=0)

    assert resp.status == 400
    assert await resp.json() == {"error": "bad_input"}


@pytest.mark.asyncio
async def test_another_matchs_action_id_replayed_here_is_bad_input(client):
    match_id = await seed()
    action_id = str(uuid.uuid4())
    async with db_session() as s:
        s.add(TeamDraftAction(
            action_id=action_id, user_id="101",
            result_json=json.dumps({"ok": True, "code": None, "match_id": match_id + 1}),
            done_utc=datetime(2026, 10, 26, 12, 10)))

    resp = await _act(client, await _auth(match_id), action_id=action_id,
                      kind="book", start_utc=START, seen_version=0)

    assert resp.status == 400


@pytest.mark.asyncio
async def test_malformed_bodies_are_bad_input(client):
    match_id = await seed()
    headers = await _auth(match_id)
    for body in (dict(kind="book", start_utc="2026-10-28T19:30:00", seen_version=0),
                 dict(kind="nope", seen_version=0),
                 dict(kind="clear", seen_version="0")):
        resp = await _act(client, headers, **body)
        assert resp.status == 400


@pytest.mark.asyncio
async def test_another_guilds_match_is_refused_by_the_api_not_the_hook(client, announce):
    # The default stub would hand back rosters for any match.
    match_id = await seed()
    headers = await _auth(match_id, guild="333")

    assert (await client.get("/api/tournament-match", headers=headers)).status == 404
    resp = await _act(client, headers, kind="book", start_utc=START, seen_version=0)
    assert resp.status == 404
    assert await resp.json() == {"error": "gone"}
    announce.assert_not_awaited()
    async with db_session() as s:
        assert (await s.get(TournamentMatch, match_id)).booking_version == 0
