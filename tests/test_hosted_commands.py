"""Draft commands on a hosted draft pass straight through to the control API.

A hosted draft has no DraftSetupManager to ask: /pause, /unpause, /scrap,
/release_draft_logs and /ready become API calls, and an admin's /replace_seat
hands one empty seat to a bot. What the poller must remember across a restart --
who paused the table, which seats a bot now plays -- is in the database, so a
restarted bot neither lifts a human's pause nor pauses for a seat a bot holds.
"""
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs import draft_control
from cogs.draft_control import DraftControlCog
from helpers.permissions import is_bot_manager
from services import hosted_draft
from services.draft_setup_manager import DraftSetupManager
from services.hosted_draftmancer import HostedRefused, HostedUnavailable

from hosted_fakes import (  # noqa: F401 -- fixtures are used by name
    DRAFT_ID, SID, SIGN_UPS, FakeChannel, _row, _seed, bot, channel, dms, everyone, hosted, seat,
)


def make_ctx(author_id: int = 111) -> MagicMock:
    ctx = MagicMock()
    ctx.author.id = author_id
    ctx.author.mention = f"<@{author_id}>"
    ctx.channel.id = 999
    ctx.channel.send = AsyncMock()
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    ctx.guild.get_member.return_value = None
    return ctx


def replies(ctx: MagicMock) -> str:
    return " ".join(str(c.args[0]) for c in ctx.followup.send.await_args_list if c.args)


def posted(ctx: MagicMock) -> str:
    return " ".join(str(c.args[0]) for c in ctx.channel.send.await_args_list if c.args)


@pytest.fixture
def cog(bot: MagicMock) -> DraftControlCog:
    return DraftControlCog(bot=bot)


@pytest.fixture
def no_manager():
    """A hosted draft must never reach for a manager."""
    with patch.object(DraftSetupManager, "get_active_manager",
                      MagicMock(side_effect=AssertionError("asked for a manager"))) as get:
        yield get


@pytest.fixture(autouse=True)
def no_vote_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(draft_control, "VOTE_ACTION_DELAY_SECONDS", 0)


async def passing_vote(ctx: Any, *, on_pass: Any, **_: Any) -> bool:
    await on_pass()
    return True


# --- /pause and /unpause ------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_on_a_hosted_draft_calls_the_api_not_a_manager(hosted, cog, no_manager):
    await _seed()
    hosted.stage = "drafting"
    ctx = make_ctx()

    await DraftControlCog.pause_command.callback(cog, ctx)

    assert hosted.calls == [("pause", DRAFT_ID)]
    assert (await _row()).hosted_paused_by == "human"
    assert "Draft paused" in posted(ctx) and "<@111>" in posted(ctx)
    no_manager.assert_not_called()


@pytest.mark.asyncio
async def test_pause_is_for_participants_only(hosted, cog, no_manager):
    await _seed()
    hosted.stage = "drafting"
    ctx = make_ctx(author_id=555)

    await DraftControlCog.pause_command.callback(cog, ctx)

    assert hosted.calls == []
    assert "participants" in replies(ctx)


@pytest.mark.asyncio
async def test_pause_before_the_draft_starts_says_so(hosted, cog, no_manager):
    await _seed()
    hosted.fail("pause", HostedRefused(409, {"error": "No draft is running."}))
    ctx = make_ctx()

    await DraftControlCog.pause_command.callback(cog, ctx)

    assert (await _row()).hosted_paused_by is None
    assert "hasn't started" in replies(ctx)


@pytest.mark.asyncio
async def test_pause_with_the_api_down_says_so_and_records_nothing(hosted, cog, no_manager):
    await _seed()
    hosted.fail("pause", HostedUnavailable("down"))
    ctx = make_ctx()

    await DraftControlCog.pause_command.callback(cog, ctx)

    assert (await _row()).hosted_paused_by is None
    assert "reach" in replies(ctx)


@pytest.mark.asyncio
async def test_the_poller_never_resumes_a_human_pause_even_after_a_restart(hosted, bot, cog, channel):
    await _seed()
    hosted.stage = "drafting"
    hosted.seats = everyone()
    await DraftControlCog.pause_command.callback(cog, make_ctx())

    hosted.seats = everyone(Bob=(False, "unknown"))   # someone drops while paused...
    for _ in range(2):
        await hosted_draft.poll_hosted_drafts(bot)
    hosted.seats = everyone()                          # ...and comes back
    await hosted_draft.poll_hosted_drafts(bot)
    hosted_draft.forget_all()                          # a restart
    await hosted_draft.poll_hosted_drafts(bot)

    assert hosted.call_names() == ["pause"]
    assert channel.messages == []
    assert (await _row()).hosted_paused_by == "human"


@pytest.mark.asyncio
async def test_unpause_on_a_hosted_draft_resumes_through_the_api_after_the_ready_check(
        hosted, cog, no_manager, monkeypatch):
    await _seed()
    hosted.stage = "drafting"
    await DraftControlCog.pause_command.callback(cog, make_ctx())

    class AllReady:
        def __init__(self, *a: Any, **k: Any) -> None:
            self.complete = MagicMock(wait=AsyncMock())
            self.message = None

        async def generate_status_embed(self, guild: Any) -> None:
            return None

        async def start_timer(self) -> None:
            return None

        def is_everyone_ready(self) -> bool:
            return True

    monkeypatch.setattr(draft_control, "DraftMancerReadyCheckView", AllReady)
    ctx = make_ctx(author_id=222)
    await DraftControlCog.unpause_command.callback(cog, ctx)

    assert hosted.call_names() == ["pause", "resume"]
    row = await _row()
    assert row.hosted_paused_by is None and row.hosted_pause_notice_id is None


@pytest.mark.asyncio
async def test_unpause_leaves_a_pause_for_an_empty_seat_to_the_poller(hosted, bot, cog, channel):
    await _seed()
    hosted.stage = "drafting"
    hosted.seats = everyone(Bob=(False, "unknown"))
    await hosted_draft.poll_hosted_drafts(bot)            # the poller pauses for Bob
    ctx = make_ctx()

    await DraftControlCog.unpause_command.callback(cog, ctx)

    assert hosted.call_names() == ["pause"]
    assert "/replace_seat" in replies(ctx)


@pytest.mark.asyncio
async def test_a_pause_for_an_empty_seat_survives_a_restart_without_a_second_notice(
        hosted, bot, channel):
    await _seed()
    hosted.stage = "drafting"
    hosted.seats = everyone(Bob=(False, "unknown"))
    await hosted_draft.poll_hosted_drafts(bot)
    [notice] = channel.sent("Draft paused")

    hosted_draft.forget_all()                              # a restart
    await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.call_names() == ["pause"]
    assert len(channel.messages) == 1

    hosted.seats = everyone()
    await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.call_names() == ["pause", "resume"]
    assert "resumed" in notice.content.lower()
    row = await _row()
    assert row.hosted_paused_by is None and row.hosted_pause_notice_id is None


# --- /scrap -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scrap_stops_the_hosted_session(hosted, cog, no_manager, monkeypatch):
    await _seed()
    hosted.stage = "drafting"
    await DraftControlCog.pause_command.callback(cog, make_ctx())
    monkeypatch.setattr(draft_control, "run_participant_vote", passing_vote)
    release = AsyncMock()
    monkeypatch.setattr(draft_control, "release_draft_pool", release)
    ctx = make_ctx()

    await cog._do_scrap(ctx)

    assert hosted.call_names() == ["pause", "stop"]
    release.assert_not_awaited()
    message = str(ctx.channel.send.return_value.edit.await_args)
    assert "Draft canceled" in message and "/ready" in message
    assert draft_control.SCRAP_KEEPS_ENTRIES not in message


@pytest.mark.asyncio
async def test_scrap_of_a_staked_hosted_draft_says_the_entries_stay(
        hosted, cog, no_manager, monkeypatch):
    await _seed()
    hosted.stage = "drafting"
    await DraftControlCog.pause_command.callback(cog, make_ctx())
    monkeypatch.setattr(draft_control, "run_participant_vote", passing_vote)
    release = AsyncMock()
    monkeypatch.setattr(draft_control, "release_draft_pool", release)
    monkeypatch.setattr(draft_control, "pool_balance", AsyncMock(return_value=50))
    ctx = make_ctx()

    await cog._do_scrap(ctx)

    release.assert_not_awaited()
    sent = ctx.channel.send.return_value.edit.await_args.kwargs["content"]
    assert draft_control.SCRAP_KEEPS_ENTRIES in sent


@pytest.mark.asyncio
async def test_scrap_that_cannot_reach_the_api_keeps_the_pool(hosted, cog, no_manager, monkeypatch):
    await _seed()
    hosted.stage = "drafting"
    await DraftControlCog.pause_command.callback(cog, make_ctx())
    hosted.fail("stop", HostedUnavailable("down"))
    monkeypatch.setattr(draft_control, "run_participant_vote", passing_vote)
    release = AsyncMock()
    monkeypatch.setattr(draft_control, "release_draft_pool", release)

    await cog._do_scrap(make_ctx())

    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_scrap_on_a_hosted_draft_still_needs_a_pause_first(hosted, cog, no_manager):
    await _seed()
    hosted.stage = "drafting"
    ctx = make_ctx()

    await cog._do_scrap(ctx)

    assert hosted.calls == []
    assert "/pause" in replies(ctx)


# --- /release_draft_logs ------------------------------------------------------------

@pytest.mark.asyncio
async def test_release_draft_logs_releases_through_the_api_and_publishes(
        hosted, cog, no_manager, monkeypatch):
    await _seed(draft_chat_channel="999")
    monkeypatch.setattr(draft_control, "run_participant_vote", passing_vote)

    async def publish(bot: Any, session_id: str, release: bool = False, *,
                      release_log: Any = None) -> bool:
        assert session_id == SID and release
        await release_log()
        return True

    with patch("services.hosted_draft.publish_draft_log", publish):
        await cog.releaselogs_command.callback(cog, make_ctx())

    assert hosted.calls == [("release_log", DRAFT_ID)]


@pytest.mark.asyncio
async def test_a_failed_release_still_publishes_the_captured_log(hosted, bot, monkeypatch):
    await _seed()
    hosted.fail("release_log", HostedRefused(409, {"error": "The draft has not ended."}))
    published = []

    async def publish(bot: Any, session_id: str, release: bool = False, *,
                      release_log: Any = None) -> bool:
        await release_log()
        published.append(session_id)
        return True

    with patch("services.hosted_draft.publish_draft_log", publish):
        assert await hosted_draft.release_hosted_logs(bot, SID) is True
    assert published == [SID]


# --- /replace_seat ------------------------------------------------------------------

def test_replace_seat_is_admin_only():
    assert is_bot_manager in DraftControlCog.replace_seat_command.checks


@pytest.mark.asyncio
async def test_replace_seat_is_admin_only_and_replaces_only_that_seat(hosted, bot, cog, channel):
    assert is_bot_manager in DraftControlCog.replace_seat_command.checks

    await _seed()
    hosted.stage = "drafting"
    hosted.seats = everyone(Bob=(False, "unknown"), Cara=(False, "unknown"))
    await hosted_draft.poll_hosted_drafts(bot)             # paused for Bob and Cara
    bob = MagicMock(id=222)
    ctx = make_ctx(author_id=1)

    await DraftControlCog.replace_seat_command.callback(cog, ctx, bob)

    assert [c for c in hosted.calls if c[0] == "replace_with_bot"] == [
        ("replace_with_bot", DRAFT_ID, "Bob")]
    assert (await _row()).hosted_bot_seats == ["Bob"]
    assert "<@222>" in posted(ctx) and "bot" in posted(ctx).lower()
    assert "<@333>" not in posted(ctx)


@pytest.mark.asyncio
async def test_a_seat_a_bot_plays_counts_as_present_to_the_poller(hosted, bot, cog, channel):
    await _seed()
    hosted.stage = "drafting"
    hosted.seats = everyone(Bob=(False, "unknown"))
    await hosted_draft.poll_hosted_drafts(bot)             # paused for Bob
    await DraftControlCog.replace_seat_command.callback(cog, make_ctx(author_id=1), MagicMock(id=222))

    # The server still reports Bob's seat disconnected: it is a bot now.
    await hosted_draft.poll_hosted_drafts(bot)
    hosted_draft.forget_all()
    await hosted_draft.poll_hosted_drafts(bot)

    assert hosted.call_names() == ["pause", "replace_with_bot", "resume"]
    assert len(channel.sent("Draft paused")) == 0          # the one notice now says resumed


@pytest.mark.asyncio
async def test_replace_seat_relays_the_servers_refusal(hosted, cog):
    await _seed()
    hosted.fail("replace_with_bot", HostedRefused(
        409, {"error": "Only a seat whose player left the running draft can be given to a bot."}))
    ctx = make_ctx(author_id=1)

    await DraftControlCog.replace_seat_command.callback(cog, ctx, MagicMock(id=222))

    assert "Only a seat whose player left" in replies(ctx)
    assert (await _row()).hosted_bot_seats is None


@pytest.mark.asyncio
async def test_replace_seat_refuses_someone_not_in_the_draft(hosted, cog):
    await _seed()
    ctx = make_ctx(author_id=1)

    await DraftControlCog.replace_seat_command.callback(cog, ctx, MagicMock(id=555))

    assert hosted.calls == []
    assert "isn't in this draft" in replies(ctx)


# --- /ready ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ready_on_a_hosted_draft_reruns_the_in_app_ready_check(
        hosted, bot, cog, channel, dms, no_manager):
    await _seed()
    hosted.seats = everyone()
    await hosted_draft.poll_hosted_drafts(bot)             # the first check
    hosted.clock["now"] += 91
    await hosted_draft.poll_hosted_drafts(bot)             # times out; waits for all-ready
    hosted.seats = everyone(Bob=(True, "not-ready"))       # a late "not ready" stalls it
    await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.call_names().count("ready_check") == 1

    ctx = make_ctx()
    await DraftControlCog.ready_command.callback(cog, ctx)

    assert hosted.call_names()[-2:] == ["set_seating", "ready_check"]
    assert "initiated" in replies(ctx).lower()
    assert len(channel.sent("Readycheck will timeout")) == 2     # a second check was posted


@pytest.mark.asyncio
async def test_ready_while_a_seat_is_empty_names_who_is_missing(hosted, bot, cog, no_manager):
    await _seed()
    hosted.seats = everyone(Dan=(False, "unknown"))
    ctx = make_ctx()

    await DraftControlCog.ready_command.callback(cog, ctx)

    assert "ready_check" not in hosted.call_names()
    assert "Dan" in replies(ctx)


@pytest.mark.asyncio
async def test_ready_after_the_draft_started_says_so(hosted, bot, cog, no_manager):
    await _seed()
    hosted.stage = "drafting"
    hosted.seats = everyone()
    ctx = make_ctx()

    await DraftControlCog.ready_command.callback(cog, ctx)

    assert hosted.calls == []
    assert "already started" in replies(ctx)


async def _ready_with(cog, stage, route_hosted, result="moved"):
    """Run /ready with the room's stage and the guild's route stubbed."""
    await _seed()
    ctx = make_ctx()
    with patch.object(draft_control.hosted_draft, "hosted_stage", AsyncMock(return_value=stage)), \
         patch.object(draft_control.config, "hosted_route_for", return_value=route_hosted), \
         patch.object(draft_control.hosted_draft, "reopen_draft",
                      AsyncMock(return_value=result)) as reopen, \
         patch.object(draft_control.hosted_draft, "request_ready_check",
                      AsyncMock(return_value=("started", []))) as check:
        await DraftControlCog.ready_command.callback(cog, ctx)
    return ctx, reopen, check


@pytest.mark.asyncio
async def test_ready_on_a_stopped_hosted_draft_reopens_on_the_guild_route(hosted, cog, no_manager):
    ctx, reopen, check = await _ready_with(cog, "stopped", False)

    reopen.assert_awaited_once()
    assert reopen.await_args.args[1:] == (SID, "manager")
    assert reopen.await_args.kwargs["reason"] == "redo"
    check.assert_not_awaited()
    assert "new room" in replies(ctx).lower()


@pytest.mark.asyncio
async def test_ready_on_a_lost_hosted_draft_reopens_hosted_when_the_guild_is_hosted(
        hosted, cog, no_manager):
    ctx, reopen, _ = await _ready_with(cog, "lost", True)

    assert reopen.await_args.args[1:] == (SID, "hosted")


@pytest.mark.asyncio
async def test_ready_on_a_live_hosted_draft_runs_the_ready_check_as_before(hosted, cog, no_manager):
    ctx, reopen, check = await _ready_with(cog, "gathering", True)

    check.assert_awaited_once()
    reopen.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_when_the_server_is_unreachable_points_at_failover(hosted, cog, no_manager):
    ctx, reopen, check = await _ready_with(cog, None, True)

    reopen.assert_not_awaited()
    check.assert_not_awaited()
    assert "/failover" in replies(ctx)


@pytest.mark.asyncio
async def test_ready_reopen_that_fails_points_at_failover_and_draft_route(hosted, cog, no_manager):
    ctx, _, _ = await _ready_with(cog, "stopped", True, result="unreachable")

    assert "/failover" in replies(ctx) and "/draft_route" in replies(ctx)


@pytest.mark.asyncio
async def test_ready_reopen_that_raises_gets_a_plain_failure_reply(hosted, cog, no_manager):
    await _seed()
    ctx = make_ctx()
    with patch.object(draft_control.hosted_draft, "hosted_stage", AsyncMock(return_value="stopped")), \
         patch.object(draft_control.hosted_draft, "reopen_draft",
                      AsyncMock(side_effect=RuntimeError("db"))):
        await DraftControlCog.ready_command.callback(cog, ctx)

    assert "went wrong" in replies(ctx)


# --- /failover ------------------------------------------------------------------------

CONFIRM_SENTENCE = "Only if your draft hasn't finished — a finished draft's result would be lost."


async def _failover(cog, *, stage="drafting", result="moved", author_id=111, manager=False,
                    route="hosted"):
    await _seed(route=route)
    ctx = make_ctx(author_id)
    with patch.object(draft_control.hosted_draft, "hosted_stage", AsyncMock(return_value=stage)), \
         patch.object(draft_control.hosted_draft, "reopen_draft",
                      AsyncMock(return_value=result)) as reopen, \
         patch("cogs.draft_control.is_bot_manager", new=AsyncMock(return_value=False)), \
         patch.object(DraftSetupManager, "get_active_manager",
                      MagicMock(return_value=MagicMock() if manager else None)):
        await DraftControlCog.failover_command.callback(cog, ctx)
    return ctx, reopen


@pytest.mark.asyncio
async def test_failover_is_for_players_or_admins(hosted, cog):
    ctx, reopen = await _failover(cog, author_id=999)

    reopen.assert_not_awaited()
    assert "players or an admin" in replies(ctx)


@pytest.mark.asyncio
async def test_failover_moves_a_reachable_hosted_draft_at_once(hosted, cog):
    ctx, reopen = await _failover(cog)

    reopen.assert_awaited_once()
    assert reopen.await_args.args[1:] == (SID, "manager")
    assert reopen.await_args.kwargs["reason"] == "failover"
    assert reopen.await_args.kwargs["requested_by"] == "<@111>"
    assert "moved" in replies(ctx).lower()


@pytest.mark.asyncio
async def test_failover_with_the_server_down_asks_first(hosted, cog):
    ctx, reopen = await _failover(cog, stage=None)

    reopen.assert_not_awaited()
    sent = ctx.followup.send.await_args
    view = sent.kwargs["view"]
    assert isinstance(view, draft_control.FailoverConfirmView)
    assert CONFIRM_SENTENCE in sent.args[0]

    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    with patch.object(draft_control.hosted_draft, "reopen_draft",
                      AsyncMock(return_value="moved")) as reopen_now:
        await view.children[0].callback(interaction)
    reopen_now.assert_awaited_once()
    assert reopen_now.await_args.args[1:] == (SID, "manager")
    assert reopen_now.await_args.kwargs["reason"] == "failover"
    assert reopen_now.await_args.kwargs["requested_by"] == "<@111>"


@pytest.mark.asyncio
async def test_failover_confirm_that_raises_gets_a_plain_failure_reply(hosted, cog):
    ctx, _ = await _failover(cog, stage=None)
    view = ctx.followup.send.await_args.kwargs["view"]
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    with patch.object(draft_control.hosted_draft, "reopen_draft",
                      AsyncMock(side_effect=RuntimeError("db"))):
        await view.children[0].callback(interaction)

    assert "went wrong" in interaction.response.edit_message.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_failover_that_raises_gets_a_plain_failure_reply(hosted, cog):
    await _seed()
    ctx = make_ctx()
    with patch.object(draft_control.hosted_draft, "hosted_stage", AsyncMock(return_value="drafting")), \
         patch.object(draft_control.hosted_draft, "reopen_draft",
                      AsyncMock(side_effect=RuntimeError("db"))):
        await DraftControlCog.failover_command.callback(cog, ctx)

    assert "went wrong" in replies(ctx)


@pytest.mark.asyncio
async def test_failover_on_a_draft_already_on_draftmancer_says_so(hosted, cog):
    ctx, reopen = await _failover(cog, route=None, manager=True)

    reopen.assert_not_awaited()
    assert "already on draftmancer.com" in replies(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("result,words", [
    ("ended", "already finished"),
    ("no-teams", "Teams haven't been formed"),
    ("bot-seats", "played by a bot"),
    ("changed", "changed"),
])
async def test_failover_replies_for_each_refusal(hosted, cog, result, words):
    ctx, _ = await _failover(cog, result=result)

    assert words in replies(ctx)
    assert "/replace_seat" not in replies(ctx)


# --- Update Cube ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_cube_on_a_gathering_hosted_queue_moves_it_to_a_new_session(
        hosted, bot, channel):
    await _seed(start=datetime.now())
    from database.db_session import AsyncSessionLocal
    from models.draft_session import DraftSession
    from sqlalchemy import update
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == SID)
                        .values(teams_start_time=datetime.now(),
                                draft_link=f"https://draft.test/?session=DB{DRAFT_ID}"))
        await s.commit()
    hosted.seats = everyone()

    assert await hosted_draft.change_hosted_cube(bot, SID, "NewCube", 3, 15) == "moved"

    row = await _row()
    assert row.draft_id != DRAFT_ID and row.cube == "NewCube"
    assert row.packs_per_player == 3 and row.cards_per_pack == 15
    assert f"DB{row.draft_id}" in row.draft_link
    [create] = [c for c in hosted.calls if c[0] == "create"]
    assert create[1]["sessionID"] == "DB" + row.draft_id
    assert create[1]["cubeCobraID"] == "NewCube"
    assert create[1]["settings"]["boostersPerPlayer"] == 3
    assert ("stop", DRAFT_ID) in hosted.calls
    assert ("set_seating", row.draft_id, list(SIGN_UPS.values())) in hosted.calls
    [notice] = channel.sent("new Draftmancer room")
    for discord_id in SIGN_UPS:
        assert f"<@{discord_id}>" in notice.content
    assert f"DB{row.draft_id}" in notice.content


@pytest.mark.asyncio
async def test_update_cube_before_teams_moves_quietly(hosted, bot, channel):
    await _seed(stage=None)
    from database.db_session import AsyncSessionLocal
    from models.draft_session import DraftSession
    from sqlalchemy import update
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == SID)
                        .values(teams_start_time=None))
        await s.commit()
    hosted.seats = everyone(connected=False)

    assert await hosted_draft.change_hosted_cube(bot, SID, "NewCube", 3, 15) == "moved"

    assert "set_seating" not in hosted.call_names()
    assert channel.messages == []


@pytest.mark.asyncio
async def test_update_cube_is_refused_once_the_hosted_draft_has_started(hosted, bot, channel):
    await _seed()
    hosted.stage = "drafting"

    assert await hosted_draft.change_hosted_cube(bot, SID, "NewCube", 3, 15) == "started"

    assert hosted.calls == []
    row = await _row()
    assert row.draft_id == DRAFT_ID and row.cube != "NewCube"


@pytest.mark.asyncio
async def test_update_cube_that_cannot_create_the_new_session_keeps_the_old(hosted, bot):
    await _seed()
    hosted.fail("create", HostedUnavailable("down"))

    assert await hosted_draft.change_hosted_cube(bot, SID, "NewCube", 3, 15) == "unreachable"

    assert "stop" not in hosted.call_names()
    assert (await _row()).draft_id == DRAFT_ID


@pytest.mark.asyncio
async def test_the_poller_keeps_polling_a_queue_that_moved_mid_tick(hosted, bot):
    """A tick that read the row before the move polls the new session, not the
    stopped one -- whose 404 would otherwise make the poller give the draft up."""
    await _seed()
    stale = await _row()
    await hosted_draft.change_hosted_cube(bot, SID, "NewCube", 3, 15)
    new_id = (await _row()).draft_id
    asked = []
    answer = hosted.status

    async def status(draft_id: str) -> Any:
        asked.append(draft_id)
        if draft_id == DRAFT_ID:
            raise HostedRefused(404, {"error": "No such session."})
        return await answer(draft_id)
    hosted.status = status

    await hosted_draft._poll_draft(bot, stale)

    assert asked == [new_id]
    assert SID not in hosted_draft._DONE


# --- the legacy route is untouched -----------------------------------------------------

@pytest.mark.asyncio
async def test_pause_on_a_manager_draft_still_uses_the_manager(hosted, cog):
    await _seed(route=None)
    manager = MagicMock(drafting=True, draftPaused=False)
    manager.emit_as_owner = AsyncMock(return_value=True)
    with patch.object(DraftSetupManager, "get_active_manager", MagicMock(return_value=manager)):
        await DraftControlCog.pause_command.callback(cog, make_ctx())

    manager.emit_as_owner.assert_awaited_once()
    assert hosted.calls == []


async def _update_cube_through_the_button(monkeypatch: pytest.MonkeyPatch, change: Any) -> MagicMock:
    """Click Update Cube and submit a new cube; returns the submit interaction."""
    from types import SimpleNamespace

    import views

    captured: dict[str, Any] = {}

    class Picker:
        def __init__(self, *a: Any, on_submit: Any, **k: Any) -> None:
            captured["on_submit"] = on_submit

        def mark_once_sent(self, interaction: Any) -> None:
            return None

    monkeypatch.setattr(views, "CubeUpdateSelectionView", Picker)
    monkeypatch.setattr(views, "update_draft_message", AsyncMock())
    monkeypatch.setattr(views, "library_cube_changed", AsyncMock())
    monkeypatch.setattr(hosted_draft, "change_hosted_cube", change)
    click = MagicMock()
    click.response.send_message = AsyncMock()
    await views.PersistentView.update_cube_callback(
        SimpleNamespace(draft_session_id=SID), click, MagicMock())

    submit = MagicMock()
    submit.response.send_message = AsyncMock()
    submit.followup.send = AsyncMock()
    await captured["on_submit"](submit, SimpleNamespace(
        cube_choice="NewCube", packs_per_player=3, cards_per_pack=15))
    return submit


@pytest.mark.asyncio
async def test_the_update_cube_button_moves_a_hosted_queue(hosted, monkeypatch, no_manager):
    await _seed(stage=None)
    no_manager.side_effect = None
    no_manager.return_value = None
    change = AsyncMock(return_value="moved")

    submit = await _update_cube_through_the_button(monkeypatch, change)

    change.assert_awaited_once_with(submit.client, SID, "NewCube", 3, 15)
    said = str(submit.followup.send.await_args)
    assert "updated to cube **NewCube**" in said and "won't be reflected" not in said


@pytest.mark.asyncio
async def test_the_update_cube_button_refuses_a_started_hosted_draft(hosted, monkeypatch, no_manager):
    await _seed()
    no_manager.side_effect = None
    no_manager.return_value = None

    submit = await _update_cube_through_the_button(monkeypatch, AsyncMock(return_value="started"))

    assert "already started" in str(submit.followup.send.await_args)
    assert (await _row()).cube != "NewCube"


# --- /abandon -------------------------------------------------------------------------

async def _abandon_hosted(cog: DraftControlCog, ctx: MagicMock) -> None:
    from conftest import make_draft_stub

    draft = make_draft_stub("teams", draftmancer_route="hosted", draft_id=DRAFT_ID)
    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=draft)), \
         patch("cogs.draft_control.is_bot_manager", new=AsyncMock(return_value=True)), \
         patch.object(DraftSetupManager, "is_drafting",
                      MagicMock(side_effect=AssertionError("asked a manager"))):
        await cog._do_abandon(ctx)


@pytest.mark.asyncio
async def test_abandon_refuses_a_drafting_hosted_draft_and_points_at_scrap(hosted, cog):
    hosted.stage = "drafting"
    ctx = make_ctx()

    await _abandon_hosted(cog, ctx)

    assert "/scrap" in replies(ctx)
    assert "Abandon this draft?" not in replies(ctx)


@pytest.mark.asyncio
async def test_abandon_refuses_when_the_hosted_server_is_unreachable(hosted, cog):
    hosted.fail("status", HostedUnavailable("down"))
    ctx = make_ctx()

    await _abandon_hosted(cog, ctx)

    assert "reach" in replies(ctx)
    assert "Abandon this draft?" not in replies(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["gathering", "ended", "stopped"])
async def test_abandon_goes_ahead_on_a_hosted_draft_not_drafting(hosted, cog, stage):
    hosted.stage = stage
    ctx = make_ctx()

    await _abandon_hosted(cog, ctx)

    assert "Abandon this draft?" in replies(ctx)


async def _abandon_fallback(cog: DraftControlCog, ctx: MagicMock, *, captured) -> None:
    from conftest import make_draft_stub

    draft = make_draft_stub("teams", draftmancer_route="fallback", draft_id=DRAFT_ID,
                            logs_captured_at=captured)
    with patch("cogs.draft_control.DraftSession.get_by_any_channel_id",
               new=AsyncMock(return_value=draft)), \
         patch("cogs.draft_control.is_bot_manager", new=AsyncMock(return_value=True)), \
         patch.object(DraftSetupManager, "is_drafting",
                      MagicMock(side_effect=AssertionError("asked a manager"))):
        await cog._do_abandon(ctx)


@pytest.mark.asyncio
async def test_abandon_refuses_a_fallback_draft_whose_log_has_not_arrived(cog):
    """draftmancer.com can't be asked whether players are still picking; only
    the captured log proves the draft ended."""
    ctx = make_ctx()

    await _abandon_fallback(cog, ctx, captured=None)

    assert "Abandon this draft?" not in replies(ctx)
    assert "draftmancer.com" in replies(ctx)


@pytest.mark.asyncio
async def test_abandon_goes_ahead_on_a_fallback_draft_with_its_log_in(cog):
    ctx = make_ctx()

    await _abandon_fallback(cog, ctx, captured=datetime(2026, 11, 25, 20, 0))

    assert "Abandon this draft?" in replies(ctx)
