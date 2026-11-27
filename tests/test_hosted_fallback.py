"""When the hosted Draftmancer cannot run a draft, the draft runs on draftmancer.com.

The fallback is a plain draftmancer.com session with no bot inside -- the state
/mutiny leaves a manager draft in. DraftBot posts the links and steps away; the
players run the room, rooms and pairings come from the board's button, and the
log is fetched afterwards from draftmancer.com's public endpoint when the
players' settings allow it.

It is taken on its own only when the hosted session cannot be created. Once one
exists, a failing server is retried and reported -- to the draft channel and,
after three minutes, once to the admins -- and an admin decides with /mutiny.
"""
import asyncio
import socket
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select, update

from cogs.draft_control import DraftControlCog
from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from services import hosted_draft, log_reconciler
from services.draft_setup_manager import DraftSetupManager
from services.hosted_draftmancer import HostedCubeImportFailed, HostedDraftmancer, HostedUnavailable

from hosted_fakes import (  # noqa: F401 -- fixtures are used by name
    DRAFT_ID, LOG, SID, SIGN_UPS, TOKEN, FakeChannel, _row, _seed, bot, channel, dms, everyone,
    hosted, seat,
)

PUBLIC = "https://draftmancer.com"
OUR_SERVER = "https://draft.draftbot.example"


def _dead_url() -> str:
    """An address nothing listens on: every request is refused at once."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def make_ctx(author_id: int = 111) -> MagicMock:
    ctx = MagicMock()
    ctx.author.id = author_id
    ctx.author.mention = f"<@{author_id}>"
    ctx.channel.id = 999
    ctx.channel.send = AsyncMock()
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    return ctx


def replies(ctx: MagicMock) -> str:
    return " ".join(str(c.args[0]) for c in ctx.followup.send.await_args_list if c.args)


@pytest.fixture
def no_manager():
    """A fallback draft must never get a manager."""
    with patch.object(DraftSetupManager, "__init__",
                      MagicMock(side_effect=AssertionError("built a manager"))) as init:
        yield init


async def _create_draft(monkeypatch: pytest.MonkeyPatch, api_url: str,
                        events: list[str] | None = None) -> tuple[Any, FakeChannel, Any]:
    """Run the real queue creation against `api_url`; returns (details, channel, manager mock)."""
    from models.session_details import SessionDetails
    from sessions.random_session import RandomSession

    monkeypatch.setenv("DRAFTMANCER_API_URL", api_url)
    monkeypatch.setenv("DRAFTMANCER_API_TOKEN", TOKEN)
    monkeypatch.setenv("DRAFTMANCER_BASE_URL", OUR_SERVER)
    monkeypatch.setenv("SCHEDULER_BASE_URL", "https://draftbot.example")
    await hosted_draft.close_hosted_client()

    posted_in = FakeChannel()
    interaction = MagicMock(spec_set=discord.Interaction)
    interaction.user = MagicMock(id=123456, display_name="Host")
    interaction.guild_id = 789012
    interaction.guild = MagicMock(id=789012)
    interaction.channel = posted_in
    interaction.response = AsyncMock()
    if events is not None:
        interaction.response.send_message = AsyncMock(
            side_effect=lambda *a, **k: events.append("board"))
        sent = posted_in.send

        async def recording_send(content: str | None = None, **kw: Any) -> Any:
            events.append("notice")
            return await sent(content, **kw)

        posted_in.send = recording_send  # type: ignore[method-assign]
    message = MagicMock(id="987654321")
    message.channel = MagicMock(id="111222333")
    interaction.original_response = AsyncMock(return_value=message)
    interaction.client = MagicMock()
    details = SessionDetails(interaction)
    details.cube_choice = "LSVCube"
    details.first_draft_id = details.draft_id
    try:
        with patch("sessions.base_session.DraftSetupManager") as manager, \
             patch("sessions.base_session.PersistentView"), \
             patch("sessions.base_session.make_message_sticky", new_callable=AsyncMock), \
             patch("sessions.base_session.get_session_deletion_hours", return_value=5), \
             patch("sessions.base_session.get_cube_thumbnail_url", return_value="https://x/t.jpg"):
            session = RandomSession(details, session_factory=AsyncSessionLocal)
            await session.create_draft_session(interaction, interaction.client)
    finally:
        await hosted_draft.close_hosted_client()
    return details, posted_in, manager


# --- creation ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_falls_back_when_the_api_is_unreachable(test_db, monkeypatch):
    hosted_draft.forget_all()
    events: list[str] = []
    details, posted_in, manager = await _create_draft(monkeypatch, _dead_url(), events)

    manager.assert_not_called()
    async with AsyncSessionLocal() as s:
        row = await s.scalar(select(DraftSession).filter_by(session_id=details.session_id))
    assert row is not None and row.draftmancer_route == "fallback"
    # A new draftmancer.com session, never our server's address.
    assert row.draft_id != details.first_draft_id
    assert details.draft_link == row.draft_link          # the board links to it
    assert row.draft_link == f"{PUBLIC}/?session=DB{row.draft_id}"
    assert row.get_draft_link_for_user("Alice").startswith(f"{PUBLIC}/?session=DB{row.draft_id}")
    [notice] = posted_in.sent("draftmancer.com")
    assert "couldn't be reached" in notice.content
    assert "LSVCube" in notice.content
    assert events == ["board", "notice"]                 # the notice follows the board


@pytest.mark.asyncio
async def test_the_board_is_posted_before_the_hosted_server_answers(test_db, monkeypatch):
    """Discord gives three seconds to answer; the hosted server may take longer."""
    events: list[str] = []

    async def slow(request: web.Request) -> web.StreamResponse:
        await asyncio.sleep(0.3)
        events.append("created")
        return web.json_response({"result": "created"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", slow)
    server = TestServer(app)
    await server.start_server()
    hosted_draft.forget_all()
    try:
        details, _, manager = await _create_draft(monkeypatch, str(server.make_url("")), events)
    finally:
        await server.close()

    assert events == ["board", "created"]
    manager.assert_not_called()
    assert (await DraftSession.get_by_session_id(details.session_id)).draftmancer_route == "hosted"


@pytest.mark.asyncio
async def test_a_cube_the_server_cannot_import_falls_back_saying_so(test_db, monkeypatch):
    async def cube_failed(request: web.Request) -> web.StreamResponse:
        return web.json_response({"error": "Could not import the cube."}, status=502)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", cube_failed)
    server = TestServer(app)
    await server.start_server()
    hosted_draft.forget_all()
    try:
        details, posted_in, manager = await _create_draft(monkeypatch, str(server.make_url("")))
    finally:
        await server.close()

    manager.assert_not_called()
    assert (await DraftSession.get_by_session_id(details.session_id)).draftmancer_route == "fallback"
    [notice] = posted_in.sent("draftmancer.com")
    assert "couldn't import the cube" in notice.content
    assert "couldn't be reached" not in notice.content
    # The server answered: that is not an outage.
    assert hosted_draft._HEALTH.failing_since is None


@pytest.mark.asyncio
async def test_the_client_tells_a_cube_import_failure_from_an_outage():
    async def cube_failed(request: web.Request) -> web.StreamResponse:
        return web.json_response({"error": "Could not import the cube."}, status=502)

    async def proxy_error(request: web.Request) -> web.StreamResponse:
        return web.Response(text="<html>Bad Gateway</html>", status=502, content_type="text/html")

    for handler, expected in ((cube_failed, HostedCubeImportFailed), (proxy_error, HostedUnavailable)):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        client = HostedDraftmancer(str(server.make_url("")), TOKEN)
        try:
            with pytest.raises(HostedUnavailable) as raised:
                await client.create({"sessionID": "DBX"})
            assert type(raised.value) is expected
            # Only the create can fail to import a cube.
            with pytest.raises(HostedUnavailable) as other:
                await client.stop("X")
            assert type(other.value) is HostedUnavailable
        finally:
            await client.close()
            await server.close()


@pytest.mark.asyncio
async def test_a_fallback_draft_never_gets_a_manager(hosted, bot, no_manager):
    from reconnect_drafts import reconnect_draft_setup_sessions

    await _seed(stage=None, route="fallback", start=datetime.now())
    assert await reconnect_draft_setup_sessions(bot) == []
    assert await DraftSetupManager.spawn_for_existing_session(SID, bot) is None


# --- /mutiny ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mutiny_on_a_hosted_draft_stops_it_before_switching(hosted, bot, channel, monkeypatch):
    monkeypatch.setenv("DRAFTMANCER_BASE_URL", OUR_SERVER)
    await _seed()
    hosted.stage = "drafting"
    order: list[str] = []
    stop = hosted.stop

    async def stopping(draft_id: str) -> None:
        order.append("stop")
        assert (await _row()).draftmancer_route == "hosted"   # not switched yet
        await stop(draft_id)

    hosted.stop = stopping  # type: ignore[method-assign]
    rooms = AsyncMock(return_value=True)
    ctx = make_ctx()
    ctx.channel = channel
    with patch("cogs.draft_control.create_rooms_and_pairings_with_fallback", rooms), \
         patch.object(DraftSetupManager, "get_active_manager",
                      MagicMock(side_effect=AssertionError("asked for a manager"))):
        await DraftControlCog.mutiny_command.callback(DraftControlCog(bot=bot), ctx)

    assert order == ["stop"]
    assert ("stop", DRAFT_ID) in hosted.calls
    row = await _row()
    assert row.draftmancer_route == "fallback"
    assert row.draft_id != DRAFT_ID
    assert row.draft_link == f"{PUBLIC}/?session=DB{row.draft_id}"
    [notice] = channel.sent("Mutiny")
    # Every player gets their new link, on draftmancer.com.
    for discord_id in SIGN_UPS:
        assert f"<@{discord_id}>" in notice.content
    assert f"{PUBLIC}/?session=DB{row.draft_id}&userName=Alice" in notice.content
    assert "may still be open" not in notice.content
    rooms.assert_awaited_once()
    # The old session's late push finds no draft.
    assert await hosted_draft.accept_pushed_log(bot, "DB" + DRAFT_ID, LOG) is False


@pytest.mark.asyncio
async def test_mutiny_with_the_server_unreachable_switches_and_warns(hosted, bot, channel):
    await _seed()
    hosted.fail("stop", HostedUnavailable("down"))
    ctx = make_ctx()
    ctx.channel = channel
    with patch("cogs.draft_control.create_rooms_and_pairings_with_fallback", AsyncMock(return_value=True)):
        await DraftControlCog.mutiny_command.callback(DraftControlCog(bot=bot), ctx)

    assert (await _row()).draftmancer_route == "fallback"
    [notice] = channel.sent("Mutiny")
    assert "may still be open" in notice.content
    assert "couldn't be reached" in replies(ctx)


@pytest.mark.asyncio
async def test_mutiny_after_the_hosted_draft_ended_changes_nothing(hosted, bot, channel):
    from services.hosted_draftmancer import HostedRefused

    await _seed()
    hosted.fail("stop", HostedRefused(409, {"error": "The draft has already ended."}))
    ctx = make_ctx()
    ctx.channel = channel
    rooms = AsyncMock(return_value=True)
    with patch("cogs.draft_control.create_rooms_and_pairings_with_fallback", rooms):
        await DraftControlCog.mutiny_command.callback(DraftControlCog(bot=bot), ctx)

    row = await _row()
    assert (row.draftmancer_route, row.draft_id) == ("hosted", DRAFT_ID)
    assert "already finished" in replies(ctx)
    assert channel.sent("Mutiny") == []
    rooms.assert_not_awaited()


@pytest.mark.asyncio
async def test_mutiny_is_for_participants_and_admins(hosted, bot, channel):
    await _seed()
    ctx = make_ctx(author_id=555)
    ctx.channel = channel
    with patch("cogs.draft_control.is_bot_manager", AsyncMock(return_value=False)):
        await DraftControlCog.mutiny_command.callback(DraftControlCog(bot=bot), ctx)

    assert hosted.calls == []
    assert (await _row()).draftmancer_route == "hosted"


@pytest.mark.asyncio
async def test_commands_on_a_fallback_draft_say_draftbot_is_not_in_the_room(hosted, bot, no_manager):
    await _seed(route="fallback")
    ctx = make_ctx()
    await DraftControlCog.mutiny_command.callback(DraftControlCog(bot=bot), ctx)
    await DraftControlCog.pause_command.callback(DraftControlCog(bot=bot), ctx)
    assert replies(ctx).count("draftmancer.com") == 2
    assert hosted.calls == []


# --- the fallback's log -------------------------------------------------------------

class PublicDraftmancer:
    """draftmancer.com's public GET /api/getDraftLog/<session>."""

    def __init__(self) -> None:
        self.status = 200
        self.log: dict[str, Any] = LOG
        self.paths: list[str] = []

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.paths.append(request.path)
        if self.status != 200:
            return web.Response(status=self.status)
        return web.json_response(self.log)


@pytest_asyncio.fixture
async def public(monkeypatch):
    site = PublicDraftmancer()
    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", site.handle)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("PUBLIC_DRAFTMANCER_URL", str(server.make_url("")).rstrip("/"))
    yield site
    await server.close()


async def _seed_fallback_after_teams() -> None:
    await _seed(route="fallback")
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == SID)
                        .values(teams_start_time=datetime.now()))
        await s.commit()


@pytest.mark.asyncio
async def test_a_fallback_drafts_log_is_fetched_from_draftmancer_com_when_allowed(
        hosted, bot, public, no_manager):
    await _seed_fallback_after_teams()
    finish = AsyncMock()
    with patch("services.hosted_draft.finish_draft", finish):
        await log_reconciler.reconcile_capture(bot)

    assert public.paths == [f"/api/getDraftLog/DB{DRAFT_ID}"]
    finish.assert_awaited_once_with(bot, SID, LOG)
    assert hosted.calls == []   # never our server


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 500])
async def test_a_fallback_log_that_is_withheld_is_left_for_later(hosted, bot, public, no_manager, status):
    await _seed_fallback_after_teams()
    public.status = status
    finish = AsyncMock()
    with patch("services.hosted_draft.finish_draft", finish):
        await log_reconciler.reconcile_capture(bot)
        public.status = 200
        await log_reconciler.reconcile_capture(bot)   # allowed later, inside the window

    assert len(public.paths) == 2
    finish.assert_awaited_once_with(bot, SID, LOG)


@pytest.mark.asyncio
async def test_a_still_delayed_fallback_log_is_not_taken_for_the_log(hosted, bot, public, no_manager):
    """While delayed, draftmancer.com answers 200 with a log stripped of every pick."""
    await _seed_fallback_after_teams()
    public.log = {**LOG, "delayed": True, "users": {}}
    finish = AsyncMock()
    with patch("services.hosted_draft.finish_draft", finish):
        await log_reconciler.reconcile_capture(bot)
    finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_draftmancer_com_being_down_never_breaks_the_reconciler(hosted, bot, monkeypatch, no_manager):
    await _seed_fallback_after_teams()
    monkeypatch.setenv("PUBLIC_DRAFTMANCER_URL", _dead_url())
    finish = AsyncMock()
    with patch("services.hosted_draft.finish_draft", finish):
        await log_reconciler.reconcile_capture(bot)
    finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_fallback_drafts_public_embed_publishes_without_a_manager(hosted, bot, no_manager):
    await _seed(route="fallback")
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == SID).values(
            logs_captured_at=datetime.now(), unlock_at=datetime(2026, 1, 1), data_received=False))
        await s.commit()
    publish = AsyncMock()
    with patch("services.log_reconciler.publish_draft_log", publish), \
         patch("services.log_reconciler.post_team_logs", AsyncMock()), \
         patch("services.log_reconciler.assign_drafted_decks", AsyncMock()):
        await log_reconciler.reconcile_publish_and_team_logs(bot)
    publish.assert_awaited_once_with(bot, SID)


# --- the outage alert --------------------------------------------------------------------

class ToggleApi:
    """The control API, answering 503 to everything while `up` is False."""

    def __init__(self) -> None:
        self.up = False

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if not self.up:
            return web.Response(status=503)
        if request.path == "/healthCheck":
            return web.Response(status=200)
        seats = [{"name": n, "userID": "u" + n, "connected": True, "disconnectedSince": None,
                  "ready": "unknown"} for n in SIGN_UPS.values()]
        return web.json_response({"stage": "drafting", "seats": seats, "pickNumber": 3})


@pytest_asyncio.fixture
async def api(test_db, monkeypatch):
    toggle = ToggleApi()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", toggle.handle)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("DRAFTMANCER_API_URL", str(server.make_url("")))
    monkeypatch.setenv("DRAFTMANCER_API_TOKEN", TOKEN)
    monkeypatch.setenv("SERVE_ALERT_DISCORD_ID", "4242")
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(hosted_draft, "_now", lambda: clock["now"])
    toggle.clock = clock  # type: ignore[attr-defined]
    hosted_draft.forget_all()
    await hosted_draft.close_hosted_client()
    yield toggle
    await hosted_draft.close_hosted_client()
    hosted_draft.forget_all()
    await server.close()


async def _ticks(api: ToggleApi, bot: Any, n: int) -> None:
    for _ in range(n):
        await hosted_draft.poll_hosted_drafts(bot)
        api.clock["now"] += hosted_draft.POLL_INTERVAL_SECONDS  # type: ignore[attr-defined]


def _alerts(sent: AsyncMock) -> list[str]:
    return [c.args[2] for c in sent.await_args_list if c.args[1] == "4242"]


@pytest.mark.asyncio
async def test_an_outage_alerts_the_admins_once(api, bot, channel):
    await _seed(stage="pairings")
    sent = AsyncMock(return_value=True)
    with patch("services.hosted_draft.send_dm", sent):
        await _ticks(api, bot, 60)                    # five minutes down

    [alert] = _alerts(sent)
    assert "3 minutes" in alert and "/mutiny" in alert
    # The draft's channel is told once too.
    assert len(channel.sent("drafting server isn't answering")) == 1


@pytest.mark.asyncio
async def test_no_alert_before_three_minutes(api, bot, channel):
    await _seed()
    sent = AsyncMock(return_value=True)
    with patch("services.hosted_draft.send_dm", sent):
        await _ticks(api, bot, 30)                    # 150 s
    sent.assert_not_awaited()
    assert channel.sent("drafting server isn't answering") == []


@pytest.mark.asyncio
async def test_a_recovery_resets_the_alert(api, bot, channel):
    await _seed()
    sent = AsyncMock(return_value=True)
    with patch("services.hosted_draft.send_dm", sent):
        await _ticks(api, bot, 40)                    # outage one
        api.up = True
        await _ticks(api, bot, 2)
        api.up = False
        await _ticks(api, bot, 40)                    # outage two

    assert [("answering again" in t) for t in _alerts(sent)] == [False, True, False]
    assert len(channel.sent("drafting server isn't answering")) == 2
    assert len(channel.sent("answering again")) == 1


@pytest.mark.asyncio
async def test_a_short_blip_alerts_nobody(api, bot, channel):
    await _seed()
    sent = AsyncMock(return_value=True)
    with patch("services.hosted_draft.send_dm", sent):
        for _ in range(4):                            # down 2.5 min, up, down again...
            await _ticks(api, bot, 30)
            api.up = True
            await _ticks(api, bot, 1)
            api.up = False
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_outage_with_no_hosted_draft_still_alerts_once(api, bot):
    """A failed create leaves no draft to poll: the poller asks the server itself."""
    sent = AsyncMock(return_value=True)
    hosted_draft._observe_api("unanswered")                  # a create that could not connect
    with patch("services.hosted_draft.send_dm", sent):
        await _ticks(api, bot, 60)
        api.up = True
        await _ticks(api, bot, 1)
    assert [("answering again" in t) for t in _alerts(sent)] == [False, True]


@pytest.mark.asyncio
async def test_an_undelivered_alert_is_tried_again(api, bot):
    await _seed()
    sent = AsyncMock(side_effect=[False, True])
    with patch("services.hosted_draft.send_dm", sent):
        await _ticks(api, bot, 60)
    assert sent.await_count == 2


@pytest.mark.asyncio
async def test_while_the_server_is_known_down_a_new_queue_goes_straight_to_the_fallback(hosted):
    hosted_draft._observe_api("unanswered")
    hosted.clock["now"] += hosted_draft.OUTAGE_ALERT_AFTER_SECONDS
    await _seed(stage=None, route=None)
    await hosted_draft.open_draft_room(await _row(), channel=None)
    assert (await _row()).draftmancer_route == "fallback"
    assert "create" not in hosted.call_names()


@pytest.mark.asyncio
async def test_a_gathering_hosted_queue_hears_of_an_outage_once(api, bot, channel):
    await _seed(stage=None)
    with patch("services.hosted_draft.send_dm", AsyncMock(return_value=True)):
        await _ticks(api, bot, 60)
    assert len(channel.sent("drafting server isn't answering")) == 1


@pytest.mark.asyncio
async def test_a_notice_that_could_not_be_posted_is_posted_later(api, bot, channel):
    await _seed()
    send = channel.send
    failures = {"left": 1}

    async def flaky_send(content: str | None = None, **kw: Any) -> Any:
        if failures["left"]:
            failures["left"] -= 1
            raise discord.HTTPException(MagicMock(status=500), "Discord is down")
        return await send(content, **kw)

    channel.send = flaky_send  # type: ignore[method-assign]
    with patch("services.hosted_draft.send_dm", AsyncMock(return_value=True)):
        await _ticks(api, bot, 60)
    assert len(channel.sent("drafting server isn't answering")) == 1


class TokenApi:
    """The control API, refusing our token while `refusing` is True."""

    def __init__(self) -> None:
        self.refusing = True

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if self.refusing:
            return web.Response(status=401)
        return web.json_response({"result": "created"})


@pytest.mark.asyncio
async def test_a_refused_token_alerts_the_admins_once_and_recovery_resets_it(test_db, bot, monkeypatch):
    token_api = TokenApi()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", token_api.handle)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("DRAFTMANCER_API_URL", str(server.make_url("")))
    monkeypatch.setenv("DRAFTMANCER_API_TOKEN", "wrong")
    monkeypatch.setenv("SERVE_ALERT_DISCORD_ID", "4242")
    hosted_draft.forget_all()
    await hosted_draft.close_hosted_client()
    sent = AsyncMock(return_value=True)

    async def new_queue() -> str:
        await _seed(stage=None, route=None)
        route = await hosted_draft.open_draft_room(await _row(), channel=None)
        await hosted_draft.poll_hosted_drafts(bot)
        return route

    try:
        with patch("services.hosted_draft.send_dm", sent):
            assert [await new_queue() for _ in range(3)] == ["fallback"] * 3   # drafts still run
            token_api.refusing = False
            assert await new_queue() == "hosted"
            token_api.refusing = True
            assert await new_queue() == "fallback"
    finally:
        await hosted_draft.close_hosted_client()
        hosted_draft.forget_all()
        await server.close()

    texts = _alerts(sent)
    assert [("refusing" in t, "accepts" in t) for t in texts] == [
        (True, False), (False, True), (True, False)]
