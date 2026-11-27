import socket

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from services.hosted_draftmancer import (
    HostedDraftmancer,
    HostedLogStopped,
    HostedRefused,
    HostedUnavailable,
    build_session_spec,
)

TOKEN = "s3cret"


class FakeApi:
    """Stands in for the fork's control API; records requests, answers from `replies`."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict[str, str], object]] = []
        self.replies: dict[tuple[str, str], tuple[int, object]] = {}

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json() if request.can_read_body else None
        self.requests.append((request.method, request.raw_path, dict(request.headers), body))
        status, payload = self.replies.get((request.method, request.raw_path), (200, {"result": "ok"}))
        if payload is None:
            return web.Response(status=status)
        return web.json_response(payload, status=status)


@pytest_asyncio.fixture
async def api():
    fake = FakeApi()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    server = TestServer(app)
    await server.start_server()
    fake.url = str(server.make_url(""))  # type: ignore[attr-defined]
    yield fake
    await server.close()


@pytest_asyncio.fixture
async def client(api):
    c = HostedDraftmancer(api.url, TOKEN)
    yield c
    await c.close()


@pytest.mark.asyncio
async def test_sends_the_bearer_token(api, client):
    await client.ready_check("abc")
    assert api.requests[0][2]["Authorization"] == f"Bearer {TOKEN}"
    assert api.requests[0][:2] == ("POST", "/api/hosted/sessions/DBabc/ready-check")


def test_spec_is_what_draftbot_sets_today():
    spec = build_session_spec(
        "abc",
        ["a", "b"],
        "mycube",
        packs_per_player=3,
        cards_per_pack=15,
        pick_timer=60,
        unlock_timer_minutes=180,
        log_webhook="https://hook",
    )
    assert spec == {
        "sessionID": "DBabc",
        "players": ["a", "b"],
        "cubeCobraID": "mycube",
        "settings": {
            "teamDraft": True,
            "pickTimer": 60,
            "boostersPerPlayer": 3,
            "cardsPerBooster": 15,
            "draftLogUnlockTimer": 180,
            "maxPlayers": 10,
        },
        "logWebhook": "https://hook",
    }


@pytest.mark.asyncio
async def test_create_puts_the_spec(api, client):
    spec = build_session_spec("abc", ["a"], "c", 3, 15, 60, 180)
    await client.create(spec)
    method, path, _, body = api.requests[0]
    assert (method, path) == ("PUT", "/api/hosted/sessions/DBabc")
    assert body == spec and body["logWebhook"] is None


@pytest.mark.asyncio
async def test_create_conflict_is_refused(api, client):
    api.replies[("PUT", "/api/hosted/sessions/DBabc")] = (409, {"error": "A different session exists."})
    with pytest.raises(HostedRefused) as err:
        await client.create(build_session_spec("abc", ["a"], "c", 3, 15, 60, 180))
    assert err.value.status == 409


@pytest.mark.asyncio
async def test_start_returns_missing_seats_on_409(api, client):
    api.replies[("POST", "/api/hosted/sessions/DBabc/start")] = (409, {"missing": ["bob", "cy"]})
    assert await client.start("abc") == ["bob", "cy"]


@pytest.mark.asyncio
async def test_start_returns_empty_when_started(api, client):
    assert await client.start("abc") == []


@pytest.mark.asyncio
async def test_start_other_409_is_refused(api, client):
    api.replies[("POST", "/api/hosted/sessions/DBabc/start")] = (409, {"error": "The draft has already started."})
    with pytest.raises(HostedRefused):
        await client.start("abc")


@pytest.mark.asyncio
async def test_status_converts_disconnected_since_to_seconds(api, client):
    api.replies[("GET", "/api/hosted/sessions/DBabc")] = (
        200,
        {
            "sessionID": "DBabc",
            "stage": "stopped",
            "pickNumber": 4,
            "seats": [
                {"name": "a", "userID": "u1", "connected": False, "disconnectedSince": 1_700_000_000_500, "ready": "ready"},
                {"name": "b", "userID": "u2", "connected": True, "disconnectedSince": None, "ready": "unknown"},
            ],
        },
    )
    status = await client.status("abc")
    assert status.stage == "stopped" and status.pick_number == 4
    assert status.seats[0].disconnected_since == 1_700_000_000.5
    assert status.seats[0].ready == "ready" and status.seats[0].user_id == "u1"
    assert status.seats[1].disconnected_since is None and status.seats[1].connected


@pytest.mark.asyncio
async def test_status_404_is_refused(api, client):
    api.replies[("GET", "/api/hosted/sessions/DBabc")] = (404, None)
    with pytest.raises(HostedRefused) as err:
        await client.status("abc")
    assert err.value.status == 404


@pytest.mark.asyncio
async def test_bodies_and_paths_of_the_other_calls(api, client):
    await client.set_players("abc", ["a", "b"])
    await client.set_seating("abc", ["b", "a"])
    await client.replace_with_bot("abc", "Bob Smith")
    await client.remove_user("abc", "u1")
    for call in (client.pause, client.resume, client.stop, client.release_log):
        await call("abc")
    got = [(m, p, b) for m, p, _, b in api.requests]
    assert got == [
        ("PUT", "/api/hosted/sessions/DBabc/players", {"players": ["a", "b"]}),
        ("PUT", "/api/hosted/sessions/DBabc/seating", {"order": ["b", "a"]}),
        ("POST", "/api/hosted/sessions/DBabc/seats/Bob%20Smith/replace-with-bot", None),
        ("DELETE", "/api/hosted/sessions/DBabc/users/u1", None),
        ("POST", "/api/hosted/sessions/DBabc/pause", None),
        ("POST", "/api/hosted/sessions/DBabc/resume", None),
        ("POST", "/api/hosted/sessions/DBabc/stop", None),
        ("POST", "/api/hosted/sessions/DBabc/release-log", None),
    ]


@pytest.mark.asyncio
async def test_unauthorised_is_refused(api, client):
    api.replies[("POST", "/api/hosted/sessions/DBabc/pause")] = (401, None)
    with pytest.raises(HostedRefused) as err:
        await client.pause("abc")
    assert err.value.status == 401


@pytest.mark.asyncio
async def test_server_errors_are_unavailable(api, client):
    api.replies[("POST", "/api/hosted/sessions/DBabc/pause")] = (502, {"error": "x"})
    with pytest.raises(HostedUnavailable):
        await client.pause("abc")


@pytest.mark.asyncio
async def test_a_refused_connection_raises_unavailable():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    c = HostedDraftmancer(f"http://127.0.0.1:{port}", TOKEN)
    try:
        with pytest.raises(HostedUnavailable):
            await c.status("abc")
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_fetch_log_returns_the_log(api, client):
    api.replies[("GET", "/api/hosted/sessions/DBabc/log")] = (200, {"users": {}})
    assert await client.fetch_log("abc") == {"users": {}}


@pytest.mark.asyncio
async def test_fetch_log_returns_none_on_404(api, client):
    api.replies[("GET", "/api/hosted/sessions/DBabc/log")] = (404, None)
    assert await client.fetch_log("abc") is None


@pytest.mark.asyncio
async def test_fetch_log_raises_stopped_on_410(api, client):
    api.replies[("GET", "/api/hosted/sessions/DBabc/log")] = (410, {"error": "stopped"})
    with pytest.raises(HostedLogStopped):
        await client.fetch_log("abc")


# --- configuration is read when asked, never at import -------------------------

def test_draftmancer_settings_are_read_when_asked(monkeypatch):
    import config

    monkeypatch.setenv("DRAFTMANCER_BASE_URL", "https://draft.example")
    monkeypatch.setenv("DRAFTMANCER_API_URL", "http://api.example:3000")
    monkeypatch.setenv("DRAFTMANCER_API_TOKEN", "tok")
    assert config.get_draftmancer_base_url() == "https://draft.example"
    assert config.get_draftmancer_session_url("X1") == "https://draft.example/?session=DBX1"
    assert config.draftmancer_api_url() == "http://api.example:3000"
    assert config.draftmancer_api_token() == "tok"
    assert config.hosted_draftmancer_enabled()

    monkeypatch.delenv("DRAFTMANCER_API_TOKEN")
    monkeypatch.delenv("DRAFTMANCER_BASE_URL")
    assert not config.hosted_draftmancer_enabled()
    assert config.get_draftmancer_base_url() == "https://draftmancer.com"


def test_importing_config_does_not_load_dotenv():
    import importlib
    from unittest.mock import patch

    import config

    with patch("dotenv.load_dotenv") as load:
        importlib.reload(config)
    load.assert_not_called()
    assert not hasattr(config, "DRAFTMANCER_API_TOKEN")
