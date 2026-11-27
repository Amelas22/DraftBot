"""reopen_draft: a draft moves to a new Draftmancer room, keeping its teams and entries.

/failover sends a draft to a bot-run room on draftmancer.com (route NULL, a
DraftSetupManager in the room); /ready on a hosted guild opens a new hosted
session. Either way the old room is closed first when the server can be asked,
the row swaps to a new draft id only if nobody else moved it meanwhile, and
every player is handed a new link. Teams, sign-ups and the staked pool are
never touched.
"""
import asyncio
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import update

from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from services import hosted_draft
from services.draft_setup_manager import ACTIVE_MANAGERS, DraftSetupManager
from services.hosted_draftmancer import HostedRefused, HostedUnavailable

from helpers.hosted_seats import seat_list
from hosted_fakes import (  # noqa: F401 -- fixtures are used by name
    DRAFT_ID, SID, SIGN_UPS, _row, _seed, bot, channel, dms, hosted,
)

PUBLIC = "https://draftmancer.com"
OUR_SERVER = "https://draft.draftbot.example"
TEAM_A = ["111", "222"]
TEAM_B = ["333", "444"]
STARTED = datetime(2026, 11, 26, 12, 0)


async def _set(**values: Any) -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == SID).values(**values))
        await s.commit()


async def _seed_teams(stage: str | None = "teams", route: str | None = "hosted", **values: Any) -> None:
    await _seed(stage=stage, route=route)
    await _set(team_a=TEAM_A, team_b=TEAM_B, teams_start_time=STARTED, **values)


@pytest.fixture
def spawn():
    with patch.object(DraftSetupManager, "spawn_for_existing_session", AsyncMock()) as s:
        yield s


@pytest.fixture
def admin_dms(monkeypatch):
    monkeypatch.setenv("SERVE_ALERT_DISCORD_ID", "4242")
    with patch("services.hosted_draft.send_dm", AsyncMock(return_value=True)) as sent:
        yield sent


async def _failover(bot, expected: str = DRAFT_ID) -> str:
    return await hosted_draft.reopen_draft(bot, SID, "manager", expected_draft_id=expected,
                                           reason="failover", requested_by="Mod Molly")


async def _redo(bot, requested_by: str | None = None) -> str:
    return await hosted_draft.reopen_draft(bot, SID, "hosted", expected_draft_id=DRAFT_ID,
                                           reason="redo", requested_by=requested_by)


@pytest.mark.asyncio
async def test_failover_moves_a_drafting_hosted_draft_to_a_bot_run_room(hosted, bot, spawn, admin_dms):
    await _seed_teams()
    hosted.stage = "drafting"

    assert await _failover(bot) == "moved"

    assert ("stop", DRAFT_ID) in hosted.calls
    row = await _row()
    assert row.draftmancer_route is None
    assert row.draft_id != DRAFT_ID
    assert row.draft_link.startswith(PUBLIC)
    spawn.assert_awaited_once_with(SID, bot)


@pytest.mark.asyncio
async def test_failover_keeps_teams_and_entries(hosted, bot, spawn, admin_dms):
    await _seed_teams(stage="pairings")
    hosted.stage = "drafting"
    released = AsyncMock()
    with patch("services.draft_pool_service.release_draft_pool", released):
        assert await _failover(bot) == "moved"

    released.assert_not_awaited()
    row = await _row()
    assert (row.team_a, row.team_b) == (TEAM_A, TEAM_B)
    assert row.sign_ups == SIGN_UPS
    assert row.teams_start_time == STARTED
    assert row.session_stage == "pairings"


@pytest.mark.asyncio
async def test_failover_announces_every_new_link_and_tells_the_admins(hosted, bot, channel, spawn,
                                                                     admin_dms):
    await _seed_teams()
    hosted.stage = "drafting"

    assert await _failover(bot) == "moved"

    row = await _row()
    [notice] = channel.sent("no longer work")
    for discord_id, name in SIGN_UPS.items():
        link = row.get_draft_link_for_user(name, user_id=discord_id)
        assert f"<@{discord_id}> — [your new draft link]({link})" in notice.content
    assert "bot-run room on draftmancer.com" in notice.content
    [alert] = [c.args[2] for c in admin_dms.await_args_list if c.args[1] == "4242"]
    assert SID in alert and "Mod Molly" in alert


@pytest.mark.asyncio
async def test_failover_with_the_server_unreachable_moves_anyway(hosted, bot, channel, spawn, admin_dms):
    await _seed_teams()
    hosted.fail("status", HostedUnavailable("down"))
    hosted.fail("stop", HostedUnavailable("down"))

    assert await _failover(bot) == "moved-unstopped"

    row = await _row()
    assert row.draftmancer_route is None and row.draft_id != DRAFT_ID
    spawn.assert_awaited_once()


@pytest.mark.asyncio
async def test_failover_of_an_ended_draft_is_refused(hosted, bot, spawn, admin_dms):
    await _seed_teams()
    hosted.stage = "drafting"
    hosted.fail("stop", HostedRefused(409, {"error": "The draft has already ended."}))

    assert await _failover(bot) == "ended"

    row = await _row()
    assert (row.draftmancer_route, row.draft_id) == ("hosted", DRAFT_ID)
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_failover_of_a_draft_the_server_says_ended_is_refused(hosted, bot, spawn, admin_dms):
    await _seed_teams()
    hosted.stage = "ended"

    assert await _failover(bot) == "ended"

    assert (await _row()).draft_id == DRAFT_ID
    assert hosted.calls == []


@pytest.mark.asyncio
async def test_failover_of_a_captured_draft_is_refused_without_asking(hosted, bot, spawn, admin_dms):
    await _seed_teams(logs_captured_at=datetime.now())
    hosted.fail("status", AssertionError("asked the server"))

    assert await _failover(bot) == "ended"

    assert hosted.calls == []
    assert (await _row()).draft_id == DRAFT_ID


@pytest.mark.asyncio
async def test_failover_needs_teams(hosted, bot, spawn, admin_dms):
    await _seed_teams(stage=None)

    assert await _failover(bot) == "no-teams"
    assert hosted.calls == []


@pytest.mark.asyncio
async def test_failover_refuses_bot_played_seats(hosted, bot, spawn, admin_dms):
    await _seed_teams(hosted_bot_seats=["Ada"])
    hosted.stage = "drafting"

    assert await _failover(bot) == "bot-seats"

    row = await _row()
    assert (row.draftmancer_route, row.draft_id) == ("hosted", DRAFT_ID)
    assert hosted.calls == []


@pytest.mark.asyncio
async def test_failover_of_a_scrapped_draft_opens_the_bot_run_room(hosted, bot, spawn, admin_dms):
    await _seed_teams()
    hosted.stage = "stopped"
    hosted.fail("stop", HostedRefused(404, {"error": "No such session."}))

    assert await _failover(bot) == "moved"

    row = await _row()
    assert row.draftmancer_route is None and row.draft_id != DRAFT_ID
    spawn.assert_awaited_once_with(SID, bot)


@pytest.mark.asyncio
async def test_a_missing_draft_is_gone(hosted, bot, spawn):
    assert await _failover(bot) == "gone"


@pytest.mark.asyncio
async def test_redo_on_the_hosted_route_opens_a_new_hosted_session(hosted, bot, channel, spawn,
                                                                  admin_dms, monkeypatch):
    monkeypatch.setenv("DRAFTMANCER_BASE_URL", OUR_SERVER)
    await _seed_teams(packs_per_player=2, cards_per_pack=10, status_message_id="77",
                      ready_check_message_id="78", hosted_paused_by="bot")
    hosted_draft._DONE.add(SID)
    hosted_draft._UNPULLABLE.add(SID)

    result = await _redo(bot, "Alice")

    assert result == "moved"
    row = await _row()
    assert row.draftmancer_route == "hosted"
    assert row.draft_id != DRAFT_ID
    assert row.draft_link == f"{OUR_SERVER}/?session=DB{row.draft_id}"
    assert (row.status_message_id, row.ready_check_message_id, row.hosted_paused_by) == (None, None, None)
    [spec] = [c[1] for c in hosted.calls if c[0] == "create"]
    seats = seat_list(SIGN_UPS)
    assert spec["sessionID"] == f"DB{row.draft_id}"
    assert spec["cubeCobraID"] == "TestCube"
    assert (spec["settings"]["boostersPerPlayer"], spec["settings"]["cardsPerBooster"]) == (2, 10)
    assert spec["players"] == seats
    assert ("set_seating", row.draft_id, seats) in hosted.calls
    assert ("stop", DRAFT_ID) in hosted.calls
    assert SID not in hosted_draft._DONE
    assert SID not in hosted_draft._UNPULLABLE
    assert SID not in hosted_draft._TRACKED
    [notice] = channel.sent("no longer work")
    assert "opened a new Draftmancer room" in notice.content
    spawn.assert_not_awaited()
    admin_dms.assert_not_awaited()


@pytest.mark.asyncio
async def test_redo_whose_create_fails_leaves_the_row_alone(hosted, bot, spawn, admin_dms):
    await _seed_teams()
    hosted.fail("create", HostedUnavailable("down"))

    result = await _redo(bot)

    assert result == "unreachable"
    row = await _row()
    assert (row.draftmancer_route, row.draft_id) == ("hosted", DRAFT_ID)


@pytest.mark.asyncio
async def test_a_lost_swap_stops_the_new_room(hosted, bot, channel, spawn, admin_dms):
    await _seed_teams()
    created: list[str] = []

    async def create(spec: dict[str, Any]) -> None:
        created.append(spec["sessionID"].removeprefix("DB"))
        await _set(draft_id="SOMEONE")     # another reopen won meanwhile

    hosted.create = create  # type: ignore[method-assign]

    result = await _redo(bot)

    assert result == "changed"
    [new_id] = created
    assert ("stop", new_id) in hosted.calls
    assert (await _row()).draft_id == "SOMEONE"
    assert channel.sent("no longer work") == []


@pytest.mark.asyncio
async def test_two_concurrent_failovers_open_one_room(hosted, bot, channel, spawn, admin_dms):
    """Two players confirm /failover at once: the second finds the draft already moved."""
    await _seed_teams()
    hosted.stage = "drafting"

    results = await asyncio.gather(_failover(bot), _failover(bot))

    assert sorted(results) == ["changed", "moved"]
    spawn.assert_awaited_once_with(SID, bot)
    assert len(channel.sent("no longer work")) == 1
    assert hosted.calls.count(("stop", DRAFT_ID)) == 1


@pytest.mark.asyncio
async def test_a_stale_failover_after_a_completed_move_changes_nothing(hosted, bot, channel, spawn,
                                                                     admin_dms):
    """A confirm prompt still open from before the move: the room it was shown for is gone."""
    await _seed_teams()
    hosted.stage = "drafting"
    assert await _failover(bot) == "moved"
    moved = await _row()
    hosted.calls.clear()

    assert await _failover(bot) == "changed"

    row = await _row()
    assert (row.draft_id, row.draft_link, row.draftmancer_route) == (
        moved.draft_id, moved.draft_link, None)
    assert hosted.calls == []
    spawn.assert_awaited_once()
    assert len(channel.sent("no longer work")) == 1


@pytest.mark.asyncio
async def test_a_redo_after_a_failover_changes_nothing(hosted, bot, channel, spawn, admin_dms):
    """/ready read the hosted row; a /failover moved the draft before /ready got the lock."""
    await _seed_teams()
    hosted.stage = "stopped"
    assert await _failover(bot) == "moved"
    hosted.calls.clear()

    assert await _redo(bot) == "changed"

    row = await _row()
    assert row.draftmancer_route is None
    assert hosted.calls == []
    assert len(channel.sent("no longer work")) == 1


@pytest.mark.asyncio
async def test_a_row_no_longer_hosted_is_not_reopened(hosted, bot, spawn, admin_dms):
    """Same room, but the draft already runs off the hosted server (a fallback or a manager)."""
    await _seed_teams(route=None)

    assert await _failover(bot) == "changed"

    assert hosted.calls == []
    spawn.assert_not_awaited()
    assert (await _row()).draft_id == DRAFT_ID


@pytest.mark.asyncio
async def test_spawn_passes_pack_settings(test_db):
    await _seed(route=None)
    await _set(packs_per_player=2, cards_per_pack=10)
    try:
        # fetch_draft_info runs as a task of its own: left pending, it is torn
        # down mid-query in a later test and breaks that test's database session.
        with patch.object(DraftSetupManager, "keep_connection_alive", AsyncMock()), \
             patch.object(DraftSetupManager, "fetch_draft_info", AsyncMock(return_value=True)):
            manager = await DraftSetupManager.spawn_for_existing_session(SID, AsyncMock())
        assert manager is not None
        assert (manager.packs_per_player, manager.cards_per_pack) == (2, 10)
    finally:
        ACTIVE_MANAGERS.pop(SID, None)


@pytest.mark.asyncio
async def test_a_running_ready_check_is_closed_by_the_move(hosted, bot, channel, spawn, admin_dms):
    await _seed_teams()
    hosted_draft._TRACKED[SID] = hosted_draft._Tracked(
        ready_check=hosted_draft._ReadyCheck(message_id=None, deadline_message_id=None, deadline=0.0))
    ended = AsyncMock()
    with patch("services.hosted_draft._end_ready_check", ended):
        assert await _failover(bot) == "moved"

    ended.assert_awaited_once()
    assert SID not in hosted_draft._TRACKED


@pytest.mark.asyncio
@pytest.mark.parametrize("old_stage", ["stopped", "ended"])
async def test_a_pull_for_the_room_a_draft_left_changes_nothing(hosted, bot, old_stage):
    """The reconciler read the old draft id before a reopen moved the draft on."""
    await _seed_teams()
    await _set(draft_id="NEWROOM")
    hosted.stage = old_stage
    hosted.log = {"sessionID": "DB" + DRAFT_ID, "users": {}}
    finished = AsyncMock()
    with patch("services.hosted_draft.on_draft_ended", finished):
        await hosted_draft.pull_ended_draft(bot, SID, DRAFT_ID)

    assert SID not in hosted_draft._UNPULLABLE
    finished.assert_not_awaited()
    assert ("fetch_log", DRAFT_ID) not in hosted.calls


@pytest.mark.asyncio
async def test_a_pull_for_the_current_room_still_finishes_the_draft(hosted, bot):
    await _seed_teams()
    hosted.stage = "ended"
    hosted.log = {"sessionID": "DB" + DRAFT_ID, "users": {}}
    held: list[bool] = []

    async def finishing(*_: Any) -> None:
        # Finishing runs with the lock let go: it must not hold up a command.
        held.append(hosted_draft._draft_lock(SID).locked())

    finished = AsyncMock(side_effect=finishing)
    with patch("services.hosted_draft.on_draft_ended", finished):
        await hosted_draft.pull_ended_draft(bot, SID, DRAFT_ID)

    finished.assert_awaited_once_with(bot, SID, hosted.log)
    assert held == [False]
