import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select

from database.db_session import db_session
from models.team_finder import LfgRequest, LfgTeam
from services.scheduler_links import issue_link
from services.scheduler_web import build_app

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
GUILD = "222"


@pytest_asyncio.fixture
async def client(test_db):
    sent = []
    seen_rows = []

    async def notify(notice, guild_id):
        async with db_session() as session:
            seen_rows.append(
                (await session.execute(select(func.count()).select_from(LfgRequest))).scalar()
            )
        sent.append((notice, guild_id))
        return False

    async def display_name(guild_id, user_id):
        return "name" + user_id

    app = build_app(now=lambda: NOW, notify=notify, display_name=display_name, enabled=lambda g: True)
    async with TestClient(TestServer(app)) as c:
        c.sent = sent  # type: ignore[attr-defined]
        c.seen_rows = seen_rows  # type: ignore[attr-defined]
        yield c


async def _auth(user="111", view="lfg", guild=GUILD):
    url = await issue_link(user, guild, view, NOW)
    return {"Authorization": "Bearer " + url.split("#")[1]}


async def _act(client, headers, **body):
    body.setdefault("action_id", str(uuid.uuid4()))
    return await client.post("/api/teams/actions", data=json.dumps(body), headers=headers)


async def _open(client, user="111", guild=GUILD):
    resp = await _act(client, await _auth(user, guild=guild), kind="open_team", size=3, note="hi")
    assert resp.status == 200
    return (await resp.json())["state"]["me"]["on_teams"][0]


@pytest.mark.asyncio
async def test_teams_get_lists_guild_only(client):
    mine = await _open(client, "111")
    await _open(client, "333", guild="other")
    resp = await client.get("/api/teams", headers=await _auth("444"))
    body = await resp.json()
    assert [t["team_id"] for t in body["teams"]] == [mine]


@pytest.mark.asyncio
async def test_wrong_view_is_forbidden(client):
    for view in ("usual", "teams"):
        headers = await _auth(view=view)
        resp = await client.get("/api/teams", headers=headers)
        assert resp.status == 403
        assert await resp.json() == {"error": "wrong_view"}
        resp = await _act(client, headers, kind="open_team")
        assert resp.status == 403


@pytest.mark.asyncio
async def test_action_is_idempotent(client):
    team = await _open(client, "333")
    headers = await _auth("111")
    action = str(uuid.uuid4())
    first = await _act(client, headers, action_id=action, kind="request_join", team_id=team, note="x")
    second = await _act(client, headers, action_id=action, kind="request_join", team_id=team, note="x")
    assert first.status == second.status == 200
    assert await first.json() == await second.json()
    async with db_session() as session:
        count = (await session.execute(select(func.count()).select_from(LfgRequest))).scalar()
    assert count == 1
    assert len(client.sent) == 1  # a replay sends no notice


@pytest.mark.asyncio
async def test_replay_by_other_user_is_rejected(client):
    action = str(uuid.uuid4())
    await _act(client, await _auth("111"), action_id=action, kind="open_team", size=3)
    resp = await _act(client, await _auth("999"), action_id=action, kind="open_team", size=3)
    assert resp.status == 400
    assert await resp.json() == {"error": "bad_input"}


@pytest.mark.asyncio
async def test_dm_failure_does_not_fail_action(client):
    team = await _open(client, "333")
    resp = await _act(client, await _auth("111"), kind="request_join", team_id=team)
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["state"]["me"]["outgoing"][0]["status"] == "pending"
    assert len(client.sent) == 1  # attempted, returned False


@pytest.mark.asyncio
async def test_notify_raising_does_not_fail_action(test_db):
    async def boom(notice, guild_id):
        raise RuntimeError("x")

    app = build_app(now=lambda: NOW, notify=boom, enabled=lambda g: True)
    async with TestClient(TestServer(app)) as c:
        team = await _open(c, "333")
        resp = await _act(c, await _auth("111"), kind="request_join", team_id=team)
        assert resp.status == 200


@pytest.mark.asyncio
async def test_notice_sent_after_commit(client):
    team = await _open(client, "333")
    await _act(client, await _auth("111"), kind="request_join", team_id=team)
    assert client.seen_rows == [1]
    notice, guild = client.sent[0]
    assert notice.user_id == "333" and guild == GUILD


@pytest.mark.asyncio
async def test_refusal_is_200_with_code(client):
    resp = await _act(client, await _auth("111"), kind="request_join", team_id="9999")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is False and body["code"] == "team_closed"


@pytest.mark.asyncio
async def test_set_taking_requests_and_withdraw(client):
    team = await _open(client, "333")
    resp = await _act(client, await _auth("333"), kind="set_taking_requests", team_id=team, on=False)
    assert (await resp.json())["ok"] is True
    async with db_session() as session:
        assert (await session.get(LfgTeam, int(team))).taking_requests is False
    resp = await _act(client, await _auth("111"), kind="withdraw_request", team_id=team)
    assert (await resp.json())["code"] == "request_gone"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"kind": "nope"},
        {},
        {"kind": "open_team", "size": 5},
        {"kind": "open_team", "size": True},
        {"kind": "open_team", "size": "3"},
        {"kind": "open_team", "note": 5},
        {"kind": "open_team", "note": "x" * 201},
        {"kind": "request_join", "team_id": 5},
        {"kind": "set_taking_requests", "team_id": "1", "on": "yes"},
        {"action_id": "not-a-uuid", "kind": "open_team"},
        {"action_id": 7, "kind": "open_team"},
    ],
)
async def test_action_bad_kind_and_fields(client, body):
    headers = await _auth()
    if "action_id" not in body:
        body = {**body, "action_id": str(uuid.uuid4())}
    resp = await client.post("/api/teams/actions", data=json.dumps(body), headers=headers)
    assert resp.status == 400
    assert await resp.json() == {"error": "bad_input"}


@pytest.mark.asyncio
async def test_non_object_and_garbage_bodies(client):
    headers = await _auth()
    for raw in ("[1]", "nope", '"x"'):
        resp = await client.post("/api/teams/actions", data=raw, headers=headers)
        assert resp.status == 400
    resp = await client.post("/api/teams/actions", data="x" * 5000, headers=headers)
    assert resp.status == 413


@pytest.mark.asyncio
async def test_third_open_team_is_refused(client):
    await _open(client, "111")
    await _open(client, "111")
    resp = await _act(client, await _auth("111"), kind="open_team", size=3)
    body = await resp.json()
    assert body["ok"] is False and body["code"] == "too_many_teams"


@pytest.mark.asyncio
async def test_actions_on_another_guilds_team_are_refused(client):
    team = await _open(client, "333")
    elsewhere = await _auth("333", guild="other")
    resp = await _act(client, elsewhere, kind="set_taking_requests", team_id=team, on=False)
    assert (await resp.json())["code"] == "team_closed"
    resp = await _act(client, await _auth("111", guild="other"), kind="withdraw_request", team_id=team)
    assert (await resp.json())["code"] == "team_closed"
    async with db_session() as session:
        assert (await session.get(LfgTeam, int(team))).taking_requests is True


@pytest.mark.asyncio
async def test_concurrent_accepts_never_overfill(client):
    team = await _open(client, "111")
    await _act(client, await _auth("200"), kind="request_join", team_id=team)
    await _act(client, await _auth("111"), kind="accept", team_id=team, requester="200")
    for user in ("300", "400"):
        await _act(client, await _auth(user), kind="request_join", team_id=team)
    captain = await _auth("111")
    resps = await asyncio.gather(
        _act(client, captain, kind="accept", team_id=team, requester="300"),
        _act(client, captain, kind="accept", team_id=team, requester="400"),
    )
    bodies = [await r.json() for r in resps]
    assert sorted(b["ok"] for b in bodies) == [False, True]
    assert [b["code"] for b in bodies if not b["ok"]] == ["team_full"]
    state = (await (await client.get("/api/teams", headers=captain)).json())["teams"][0]
    assert len(state["members"]) == 3 and state["status"] == "full"


@pytest.mark.asyncio
async def test_captain_kinds_validate_and_run(client):
    team = await _open(client, "111")
    cap = await _auth("111")
    for bad in ({"requester": 5}, {"requester": "abc"}, {}):
        resp = await _act(client, cap, kind="accept", team_id=team, **bad)
        assert resp.status == 400
    await _act(client, await _auth("200"), kind="request_join", team_id=team)
    resp = await _act(client, cap, kind="decline", team_id=team, requester="200")
    assert (await resp.json())["ok"] is True
    resp = await _act(client, await _auth("200"), kind="leave_team", team_id=team)
    assert (await resp.json())["code"] == "bad_input"
    resp = await _act(client, cap, kind="close_team", team_id=team)
    body = await resp.json()
    assert body["ok"] is True and body["state"]["teams"] == []
    resp = await _act(client, cap, kind="close_team", team_id=team)
    assert (await resp.json())["code"] == "team_closed"


@pytest.mark.asyncio
async def test_leave_team_success_path(client):
    team = await _open(client, "111")
    member = await _auth("200")
    await _act(client, member, kind="request_join", team_id=team)
    await _act(client, await _auth("111"), kind="accept", team_id=team, requester="200")
    resp = await _act(client, member, kind="leave_team", team_id=team)
    body = await resp.json()
    assert body["ok"] is True and body["code"] is None
    assert body["state"]["me"]["on_teams"] == []
    (listed,) = body["state"]["teams"]
    assert [m["user"] for m in listed["members"]] == ["111"]
    assert any("left your team" in n.text and n.user_id == "111" for n, _ in client.sent)


@pytest.mark.asyncio
async def test_captain_actions_from_another_guilds_link_are_refused(client):
    team = await _open(client, "111")
    await _act(client, await _auth("200"), kind="request_join", team_id=team)
    elsewhere = await _auth("111", guild="other")
    for body in (
        {"kind": "accept", "team_id": team, "requester": "200"},
        {"kind": "decline", "team_id": team, "requester": "200"},
        {"kind": "close_team", "team_id": team},
        {"kind": "leave_team", "team_id": team},
    ):
        resp = await _act(client, elsewhere, **body)
        assert (await resp.json())["code"] == "team_closed", body
    state = await (await client.get("/api/teams", headers=await _auth("111"))).json()
    assert state["teams"][0]["status"] == "open"
    assert [r["user"] for r in state["me"]["incoming"][team]] == ["200"]
