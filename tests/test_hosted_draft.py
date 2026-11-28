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

from hosted_fakes import (  # noqa: F401 -- fixtures are used by name
    DRAFT_ID, LOG, SID, SIGN_UPS, TOKEN, FakeChannel, FakeHosted, _row, _seed, bot, channel, dms,
    everyone, hosted, seat,
)


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
async def test_an_unreachable_api_records_the_fallback_not_the_hosted_route(test_db, monkeypatch):
    await _seed(stage=None, route=None)
    failing = MagicMock()
    failing.create = AsyncMock(side_effect=HostedUnavailable("down"))
    monkeypatch.setattr(hosted_draft, "hosted_client", lambda: failing)
    monkeypatch.setenv("DRAFTMANCER_API_URL", "http://api.test")
    monkeypatch.setenv("DRAFTMANCER_API_TOKEN", TOKEN)

    assert await hosted_draft.open_draft_room(await _row(), channel=None) == "fallback"
    failing.create.assert_awaited_once()
    assert (await _row()).draftmancer_route == "fallback"


# --- players and seating --------------------------------------------------------

@pytest.mark.asyncio
async def test_sign_up_changes_after_teams_update_the_expected_players(hosted):
    await _seed()
    await hosted_draft.on_signups_changed(SID)
    assert hosted.calls == [("set_players", DRAFT_ID, list(SIGN_UPS.values()))]


@pytest.mark.asyncio
async def test_sign_up_changes_before_teams_leave_the_hosted_session_alone(hosted, bot, channel):
    # Nobody has a link before teams, and forming them sends every player and the seating.
    await _seed(stage=None)
    hosted.seats = [seat("Alice", False)]
    await hosted_draft.on_signups_changed(SID)
    await hosted_draft.poll_hosted_drafts(bot)
    assert "set_players" not in hosted.call_names()


@pytest.mark.asyncio
async def test_the_last_player_leaving_a_hosted_queue_sends_nothing(hosted, bot, channel):
    await _seed(stage=None)
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == SID).values(sign_ups={}))
        await s.commit()
    hosted.seats = [seat("Alice", False)]
    await hosted_draft.on_signups_changed(SID)
    await hosted_draft.poll_hosted_drafts(bot)
    assert "set_players" not in hosted.call_names()


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
    await _seed()
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


# --- a blip is not a disconnect ------------------------------------------------------

@pytest.mark.asyncio
async def test_a_disconnect_inside_the_grace_window_neither_pauses_nor_posts(hosted, bot, channel):
    from services.draft_setup_manager import DISCONNECT_NOTICE_DELAY

    await _seed()
    hosted.stage = "drafting"
    now = hosted.clock["now"]

    def bob_away_since(since: float) -> list[HostedSeat]:
        seats = everyone()
        seats[1] = HostedSeat(name="Bob", user_id="seat-Bob", connected=False,
                              disconnected_since=since, ready="unknown")
        return seats

    hosted.seats = bob_away_since(now - 2)
    await hosted_draft.poll_hosted_drafts(bot)
    hosted.clock["now"] += DISCONNECT_NOTICE_DELAY - 5
    await hosted_draft.poll_hosted_drafts(bot)
    hosted.seats = everyone()                      # back inside the window
    await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.calls == [] and channel.messages == []

    dropped = hosted.clock["now"]
    hosted.seats = bob_away_since(dropped)
    await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.calls == []
    hosted.clock["now"] = dropped + DISCONNECT_NOTICE_DELAY
    for _ in range(3):
        await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.call_names() == ["pause"]
    assert len(channel.sent("Draft paused")) == 1


# --- players who share a display name ---------------------------------------------

TWINS = {"111": "Alice", "222": "Bob", "333": "Bob", "444": "Dan"}


def test_seat_names_are_unique_and_never_another_players_name():
    from helpers.hosted_seats import seat_list, seat_names

    assert seat_list(TWINS) == ["Alice", "Bob", "Bob (2)", "Dan"]
    tricky = {"1": "Bob", "2": "Bob", "3": "Bob (2)"}
    assert seat_names(tricky) == {"1": "Bob", "2": "Bob (3)", "3": "Bob (2)"}


@pytest.mark.asyncio
async def test_two_sign_ups_with_the_same_display_name_each_get_a_seat(hosted, bot, channel, monkeypatch):
    await _seed(stage=None)
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == SID)
                        .values(sign_ups=dict(TWINS)))
        await s.commit()
    seats = ["Alice", "Bob", "Bob (2)", "Dan"]

    created: list[dict[str, Any]] = []

    async def create(spec: dict[str, Any]) -> None:
        created.append(spec)

    monkeypatch.setattr(hosted, "create", create, raising=False)
    assert await hosted_draft.open_draft_room(await _row(), channel=None) == "hosted"
    assert created[0]["players"] == seats

    # Teams: the seating is a permutation of exactly those seat names.
    from helpers.hosted_seats import seat_list
    reordered = {k: TWINS[k] for k in ("333", "111", "444", "222")}
    seating = seat_list(reordered)
    assert sorted(seating) == sorted(seats)
    await hosted_draft.on_teams_formed(SID, seating)
    assert hosted.calls[-2:] == [("set_players", DRAFT_ID, seating), ("set_seating", DRAFT_ID, seating)]

    # After teams a sign-up change sends the seat names, and the poller then sees
    # the seats in step: no endless re-sending.
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == SID)
                        .values(session_stage="teams", sign_ups=reordered))
        await s.commit()
    await hosted_draft.on_signups_changed(SID)
    assert hosted.calls[-1] == ("set_players", DRAFT_ID, seating)
    hosted.seats = [seat(name, False) for name in seating]
    before = hosted.call_names().count("set_players")
    await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.call_names().count("set_players") == before


@pytest.mark.asyncio
async def test_each_players_link_carries_their_own_seat_name(test_db):
    from urllib.parse import parse_qs, urlparse

    from services.team_creator import _create_teams_embed

    row = DraftSession(session_id=SID, draft_id=DRAFT_ID, sign_ups=dict(TWINS),
                       draft_link=f"https://draft.example/?session=DB{DRAFT_ID}",
                       draftmancer_route="hosted", session_type="random", cube="LSV")

    def user_name(link: str) -> str:
        return parse_qs(urlparse(link).query)["userName"][0]

    assert user_name(row.get_draft_link_for_user("Bob", user_id="222")) == "Bob"
    assert user_name(row.get_draft_link_for_user("Bob", user_id="333")) == "Bob (2)"

    embed = await _create_teams_embed(row, None, None, [], "random")
    links = "\n".join(f.value for f in embed.fields)
    assert "userName=Bob%20%282%29" in links and "userName=Bob)" in links

    # The manager route keeps the display name as it is.
    row.draftmancer_route = None
    assert user_name(row.get_draft_link_for_user("Bob", user_id="333")) == "Bob"


@pytest.mark.asyncio
async def test_the_pause_notice_mentions_each_twin_by_their_own_id(hosted, bot, channel):
    await _seed()
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == SID)
                        .values(sign_ups=dict(TWINS)))
        await s.commit()
    hosted.stage = "drafting"
    hosted.seats = [seat("Alice"), seat("Bob"), seat("Bob (2)", connected=False), seat("Dan")]
    await hosted_draft.poll_hosted_drafts(bot)
    [notice] = channel.sent("Draft paused")
    assert "<@333>" in notice.content and "<@222>" not in notice.content
