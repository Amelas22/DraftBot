"""A draft run on the hosted Draftmancer, from queue to finished log.

The hosted route never sits in the draft: it creates the session through the
control API, keeps the expected players and the seating in step with Discord,
and polls status to drive the channel -- the status message, the in-app ready
check, the start, and the pause while a seat is empty. The end arrives as a
pushed log (or the reconciler's pull) and runs the same end-of-draft code as the
manager, rooms at most once.
"""
import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select, update

import views
from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from services import draft_end, hosted_draft
from services.hosted_draftmancer import HostedSeat, HostedStatus, HostedUnavailable
from services.scheduler_web import build_app

from conftest import seed_session

SID = "hosted-1"
DRAFT_ID = "ABC123"
TOKEN = "s3cret"
SIGN_UPS = {"111": "Alice", "222": "Bob", "333": "Cara", "444": "Dan"}
LOG = {
    "sessionID": "DB" + DRAFT_ID,
    "time": 1700000000000,
    "users": {f"u{i}": {"userName": n, "seatNum": i, "picks": []}
              for i, n in enumerate(SIGN_UPS.values())},
}


# --- fakes --------------------------------------------------------------------

class FakeMessage:
    def __init__(self, channel: "FakeChannel", mid: int, content: str | None):
        self.channel, self.id, self.content = channel, mid, content
        self.deleted = False

    async def edit(self, content: str | None = None, **_: Any) -> None:
        self.content = content
        self.channel.edits.append((self.id, content))

    async def delete(self) -> None:
        self.deleted = True


class FakeChannel:
    def __init__(self) -> None:
        self.id = 999
        self.name = "draft-chat"
        self.guild = MagicMock(id=1)
        self.guild.name = "Guild"
        self.messages: list[FakeMessage] = []
        self.edits: list[tuple[int, str | None]] = []

    async def send(self, content: str | None = None, **_: Any) -> FakeMessage:
        message = FakeMessage(self, 1000 + len(self.messages), content)
        self.messages.append(message)
        return message

    async def fetch_message(self, mid: int) -> FakeMessage:
        for m in self.messages:
            if m.id == mid and not m.deleted:
                return m
        raise discord.NotFound(MagicMock(status=404), "gone")

    def sent(self, needle: str) -> list[FakeMessage]:
        return [m for m in self.messages if m.content and needle in m.content]


class FakeHosted:
    """Stands in for HostedDraftmancer: records calls, answers status from `seats`."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.stage = "gathering"
        self.seats: list[HostedSeat] = []
        self.start_missing: list[str] = []

    def call_names(self) -> list[str]:
        return [c[0] for c in self.calls]

    async def status(self, draft_id: str) -> HostedStatus:
        return HostedStatus(stage=self.stage, seats=list(self.seats), pick_number=None)

    async def set_players(self, draft_id: str, names: list[str]) -> None:
        self.calls.append(("set_players", draft_id, list(names)))
        self.seats = [seat(n, connected=False) for n in names]

    async def set_seating(self, draft_id: str, names: list[str]) -> None:
        self.calls.append(("set_seating", draft_id, list(names)))

    async def ready_check(self, draft_id: str) -> None:
        self.calls.append(("ready_check", draft_id))
        self.seats = [seat(s.name, s.connected) for s in self.seats]

    async def start(self, draft_id: str) -> list[str]:
        self.calls.append(("start", draft_id))
        if not self.start_missing:
            self.stage = "drafting"
        return list(self.start_missing)

    async def pause(self, draft_id: str) -> None:
        self.calls.append(("pause", draft_id))

    async def resume(self, draft_id: str) -> None:
        self.calls.append(("resume", draft_id))


def seat(name: str, connected: bool = True, ready: str = "unknown") -> HostedSeat:
    return HostedSeat(name=name, user_id="seat-" + name, connected=connected,
                      disconnected_since=None if connected else 1.0, ready=ready)


def everyone(connected: bool = True, ready: str = "unknown", **overrides: tuple[bool, str]
             ) -> list[HostedSeat]:
    seats = []
    for name in SIGN_UPS.values():
        c, r = overrides.get(name, (connected, ready))
        seats.append(seat(name, c, r))
    return seats


@pytest_asyncio.fixture
async def hosted(test_db, monkeypatch):
    fake = FakeHosted()
    monkeypatch.setattr(hosted_draft, "hosted_client", lambda: fake)
    monkeypatch.setattr(hosted_draft, "START_DELAY_SECONDS", 0)
    monkeypatch.setenv("DRAFTMANCER_API_URL", "http://api.test")
    monkeypatch.setenv("DRAFTMANCER_API_TOKEN", TOKEN)
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(hosted_draft, "_now", lambda: clock["now"])
    fake.clock = clock  # type: ignore[attr-defined]
    hosted_draft.forget_all()
    yield fake
    hosted_draft.forget_all()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def bot(channel: FakeChannel) -> MagicMock:
    b = MagicMock()
    b.get_channel.return_value = channel
    b.get_guild.return_value = MagicMock(id=1)
    return b


@pytest.fixture
def dms():
    with patch("services.hosted_draft.send_ready_check_dms", AsyncMock()) as send:
        yield send


async def _seed(stage: str | None = "teams", route: str | None = "hosted", **kw: Any) -> None:
    await seed_session(session_id=SID, guild="1", stype="random", stage=stage,
                       draft_channel_id="999", sign_ups=dict(SIGN_UPS), draft_id=DRAFT_ID, **kw)
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == SID)
                        .values(draftmancer_route=route))
        await s.commit()


async def _row() -> DraftSession:
    async with AsyncSessionLocal() as s:
        row = await s.scalar(select(DraftSession).filter_by(session_id=SID))
        assert row is not None
        return row


# --- creation -----------------------------------------------------------------

class FakeApi:
    """The fork's control API over HTTP, for the one test that crosses the wire."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Any]] = []

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json() if request.can_read_body else None
        self.requests.append((request.method, request.path, body))
        return web.json_response({"result": "created"})


@pytest.mark.asyncio
async def test_a_new_queue_creates_a_hosted_session_and_records_the_route(test_db, monkeypatch):
    from models.session_details import SessionDetails
    from sessions.random_session import RandomSession

    api = FakeApi()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", api.handle)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setenv("DRAFTMANCER_API_URL", str(server.make_url("")))
    monkeypatch.setenv("DRAFTMANCER_API_TOKEN", TOKEN)
    monkeypatch.setenv("SCHEDULER_BASE_URL", "https://draftbot.example")
    await hosted_draft.close_hosted_client()

    interaction = MagicMock(spec_set=discord.Interaction)
    interaction.user = MagicMock(id=123456, display_name="Host")
    interaction.guild_id = 789012
    interaction.guild = MagicMock(id=789012)
    interaction.response = AsyncMock()
    message = MagicMock(id="987654321")
    message.channel = MagicMock(id="111222333")
    interaction.original_response = AsyncMock(return_value=message)
    interaction.client = MagicMock()
    details = SessionDetails(interaction)
    details.cube_choice = "LSVCube"

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
        await server.close()

    manager.assert_not_called()
    async with AsyncSessionLocal() as s:
        row = await s.scalar(select(DraftSession).filter_by(session_id=details.session_id))
    assert row is not None and row.draftmancer_route == "hosted"
    method, path, body = api.requests[0]
    assert (method, path) == ("PUT", f"/api/hosted/sessions/DB{details.draft_id}")
    assert body["cubeCobraID"] == "LSVCube"
    assert body["logWebhook"] == "https://draftbot.example/hosted/draft-ended"


@pytest.mark.asyncio
async def test_an_unreachable_api_does_not_record_the_route(test_db, monkeypatch):
    await _seed(stage=None, route=None)
    failing = MagicMock()
    failing.create = AsyncMock(side_effect=HostedUnavailable("down"))
    monkeypatch.setattr(hosted_draft, "hosted_client", lambda: failing)
    monkeypatch.setenv("DRAFTMANCER_API_URL", "http://api.test")
    monkeypatch.setenv("DRAFTMANCER_API_TOKEN", TOKEN)

    assert await hosted_draft.open_hosted_draft(await _row()) is False
    assert (await _row()).draftmancer_route is None


# --- players and seating --------------------------------------------------------

@pytest.mark.asyncio
async def test_sign_up_changes_update_the_expected_players(hosted):
    await _seed(stage=None)
    await hosted_draft.on_signups_changed(SID)
    assert hosted.calls == [("set_players", DRAFT_ID, list(SIGN_UPS.values()))]


@pytest.mark.asyncio
async def test_sign_up_changes_leave_a_manager_draft_alone(hosted):
    await _seed(stage=None, route=None)
    await hosted_draft.on_signups_changed(SID)
    assert hosted.calls == []


@pytest.mark.asyncio
async def test_teams_formed_sets_the_seating_order(hosted):
    await _seed()
    seating = ["Cara", "Alice", "Dan", "Bob"]
    await hosted_draft.on_teams_formed(SID, seating)
    assert ("set_seating", DRAFT_ID, seating) in hosted.calls
    assert hosted.calls[-1] == ("set_seating", DRAFT_ID, seating)


@pytest.mark.asyncio
async def test_the_poller_brings_stale_players_back_in_step(hosted, bot, channel):
    await _seed(stage=None)
    hosted.seats = [seat("Alice", False)]
    await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.calls == [("set_players", DRAFT_ID, list(SIGN_UPS.values()))]


# --- status message -------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_poller_only_reports_while_someone_is_missing(hosted, bot, channel, dms):
    await _seed(stage=None)
    hosted.seats = everyone(connected=False)
    await hosted_draft.poll_hosted_drafts(bot)
    assert channel.messages == []          # no teams yet: nobody has a link

    await _seed()
    hosted.seats = everyone(Dan=(False, "unknown"))
    await hosted_draft.poll_hosted_drafts(bot)
    [status] = channel.sent("Draft Session Status")
    assert "Drafters in Draftmancer" in status.content and "Waiting on" in status.content
    assert "❌ Dan" in status.content
    assert (await _row()).status_message_id == str(status.id)

    await hosted_draft.poll_hosted_drafts(bot)  # nothing changed: nothing said
    assert channel.edits == []

    hosted.seats = everyone()
    await hosted_draft.poll_hosted_drafts(bot)
    assert len(channel.sent("Draft Session Status")) == 1  # one message, edited
    assert "Waiting on" not in status.content
    edits = len(channel.edits)

    hosted.stage = "drafting"
    for _ in range(3):
        await hosted_draft.poll_hosted_drafts(bot)
    assert len(channel.edits) == edits
    assert len(channel.sent("Draft Session Status")) == 1


# --- ready check ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_once_every_seat_is_connected_the_poller_runs_the_ready_check_and_posts_the_count(
        hosted, bot, channel, dms):
    await _seed()
    hosted.seats = everyone(Dan=(False, "unknown"))
    await hosted_draft.poll_hosted_drafts(bot)
    assert "ready_check" not in hosted.call_names()

    hosted.seats = everyone()
    await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.call_names().count("ready_check") == 1
    [count] = channel.sent("Readycheck in progress")
    assert "0/4 ready" in count.content
    assert channel.sent("Readycheck will timeout <t:")
    dms.assert_awaited_once()
    assert (await _row()).ready_check_message_id == str(count.id)

    hosted.seats = everyone(Alice=(True, "ready"), Bob=(True, "ready"))
    await hosted_draft.poll_hosted_drafts(bot)
    assert "2/4 ready" in count.content
    assert hosted.call_names().count("ready_check") == 1   # the same check, edited


@pytest.mark.asyncio
async def test_the_draft_starts_only_when_every_seat_has_answered_ready(hosted, bot, channel, dms):
    await _seed()
    hosted.seats = everyone()
    await hosted_draft.poll_hosted_drafts(bot)

    hosted.seats = everyone(ready="ready", Dan=(True, "unknown"))
    await hosted_draft.poll_hosted_drafts(bot)
    assert "start" not in hosted.call_names()
    assert "3/4 ready" in channel.sent("Readycheck in progress")[0].content

    hosted.seats = everyone(ready="ready")
    await hosted_draft.poll_hosted_drafts(bot)
    assert channel.sent("Draft starting in 5 seconds")
    assert hosted.call_names().count("start") == 1
    deadline = channel.sent("Readycheck will timeout")[0]
    assert deadline.deleted

    await hosted_draft.poll_hosted_drafts(bot)   # drafting now: no second start
    assert hosted.call_names().count("start") == 1


@pytest.mark.asyncio
async def test_a_not_ready_answer_or_a_leave_resets_the_ready_check(hosted, bot, channel, dms):
    await _seed()
    hosted.seats = everyone()
    await hosted_draft.poll_hosted_drafts(bot)
    first = channel.sent("Readycheck in progress")[0]

    # A leave: the check fails, and a new one runs once everyone is back.
    hosted.seats = everyone(ready="ready", Bob=(False, "unknown"))
    await hosted_draft.poll_hosted_drafts(bot)
    assert "Ready check failed" in first.content and "Bob" in first.content
    assert "start" not in hosted.call_names()
    hosted.seats = everyone()
    await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.call_names().count("ready_check") == 2
    second = channel.sent("Readycheck in progress")[-1]
    assert second is not first

    # A not-ready answer: the check fails and nothing starts.
    hosted.seats = everyone(ready="ready", Cara=(True, "not-ready"))
    await hosted_draft.poll_hosted_drafts(bot)
    assert "Ready check failed" in second.content and "Cara" in second.content
    await hosted_draft.poll_hosted_drafts(bot)
    assert "start" not in hosted.call_names()
    assert hosted.call_names().count("ready_check") == 2

    # ... and a fresh check is asked for a little later, since a player who
    # answered cannot answer the old one again.
    hosted.clock["now"] += hosted_draft.NOT_READY_RETRY_SECONDS + 1
    await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.call_names().count("ready_check") == 3


@pytest.mark.asyncio
async def test_a_ready_check_times_out_after_ninety_seconds(hosted, bot, channel, dms):
    await _seed()
    hosted.seats = everyone()
    await hosted_draft.poll_hosted_drafts(bot)
    count = channel.sent("Readycheck in progress")[0]

    hosted.seats = everyone(ready="ready", Dan=(True, "unknown"))
    hosted.clock["now"] += 91
    await hosted_draft.poll_hosted_drafts(bot)
    assert "Timed out after 90 seconds" in count.content and "Dan" in count.content
    assert "start" not in hosted.call_names()

    # Dan answers late: everyone is ready, so a new check runs, as today.
    hosted.seats = everyone(ready="ready")
    await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.call_names().count("ready_check") == 2


# --- mid-draft disconnect -------------------------------------------------------

@pytest.mark.asyncio
async def test_a_mid_draft_disconnect_pauses_and_posts_one_substitute_notice_and_reconnecting_resumes(
        hosted, bot, channel):
    await _seed()
    hosted.stage = "drafting"
    hosted.seats = everyone()
    await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.calls == []

    hosted.seats = everyone(Bob=(False, "unknown"))
    for _ in range(3):
        await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.call_names() == ["pause"]
    [notice] = channel.sent("Draft paused")
    assert "<@222>" in notice.content
    assert "substitute" in notice.content and "link" in notice.content

    hosted.seats = everyone()
    await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.call_names() == ["pause", "resume"]
    assert "resumed" in notice.content.lower()
    assert len(channel.sent("Draft paused")) == 0 and len(channel.messages) == 1


# --- the end ----------------------------------------------------------------------

@pytest_asyncio.fixture
async def webhook(bot, hosted):
    app = build_app(draft_ended=lambda wire, log: hosted_draft.accept_pushed_log(bot, wire, log))
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client
    await client.close()


@pytest.mark.asyncio
async def test_ending_twice_creates_rooms_once(hosted, bot, webhook, monkeypatch):
    await _seed()

    async def slow_create(*a: Any, **k: Any) -> bool:
        await asyncio.sleep(0.05)
        return True

    async def slow_upload(*a: Any, **k: Any) -> str:
        await asyncio.sleep(0.05)   # every finisher is past the capture guard at once
        return "team/cube-1.json"

    create = AsyncMock(side_effect=slow_create)
    upload = AsyncMock(side_effect=slow_upload)
    with patch.object(views.PersistentView, "create_rooms_pairings", create), \
         patch("services.draft_end.upload_draft_log", upload), \
         patch("services.draft_end.post_team_logs", AsyncMock()), \
         patch("services.draft_end.assign_drafted_decks", AsyncMock()):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        body = {"sessionID": "DB" + DRAFT_ID, "log": LOG}
        first, second = await asyncio.gather(
            webhook.post("/hosted/draft-ended", json=body, headers=headers),
            webhook.post("/hosted/draft-ended", json=body, headers=headers))
        assert (first.status, second.status) == (200, 200)
        # The reconciler's pull, racing the pushes.
        await asyncio.gather(hosted_draft.on_draft_ended(bot, SID, LOG),
                             hosted_draft.wait_for_finishing())

    create.assert_awaited_once()
    upload.assert_awaited_once()
    row = await _row()
    assert row.logs_captured_at is not None


@pytest.mark.asyncio
async def test_the_webhook_refuses_a_wrong_token(hosted, webhook, monkeypatch):
    await _seed()
    ended = AsyncMock()
    monkeypatch.setattr(hosted_draft, "on_draft_ended", ended)
    body = {"sessionID": "DB" + DRAFT_ID, "log": LOG}

    wrong = await webhook.post("/hosted/draft-ended", json=body,
                               headers={"Authorization": "Bearer nope"})
    missing = await webhook.post("/hosted/draft-ended", json=body)
    assert (wrong.status, missing.status) == (401, 401)
    await hosted_draft.wait_for_finishing()
    ended.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_webhook_refuses_a_session_it_does_not_know(hosted, webhook):
    await _seed()
    response = await webhook.post("/hosted/draft-ended",
                                  json={"sessionID": "DBnope", "log": LOG},
                                  headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status == 404


# --- the loop -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_failing_api_call_never_kills_the_poller(hosted, bot, monkeypatch):
    await _seed()
    ticks = 0

    async def flaky(b: Any) -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            raise HostedUnavailable("down")
        if ticks >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(hosted_draft, "poll_hosted_drafts", flaky)
    monkeypatch.setattr(hosted_draft, "POLL_INTERVAL_SECONDS", 0)
    closed = AsyncMock()
    monkeypatch.setattr(hosted_draft, "close_hosted_client", closed)
    with pytest.raises(asyncio.CancelledError):
        await hosted_draft.run_hosted_poller(bot)
    assert ticks == 3
    closed.assert_awaited_once()


# --- a restart ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_restart_spawns_no_manager_for_a_hosted_draft(hosted, bot):
    from datetime import datetime

    from reconnect_drafts import reconnect_draft_setup_sessions
    from services.draft_setup_manager import DraftSetupManager

    await _seed(stage=None, start=datetime.now())
    with patch.object(DraftSetupManager, "keep_connection_alive", AsyncMock()):
        assert await reconnect_draft_setup_sessions(bot) == []
        assert await DraftSetupManager.spawn_for_existing_session(SID, bot) is None
