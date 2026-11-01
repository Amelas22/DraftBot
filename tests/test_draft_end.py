"""The end of a draft, run without a Draftmancer manager.

finish_draft is what both routes run when a draft ends -- the manager when its
socket hears endDraft, and the hosted route when the server pushes the end or the
reconciler pulls it. Those can overlap, so rooms are claimed with a durable,
conditional write before they are built: whoever loses the claim skips the rooms
and gets on with the log.
"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

import views
from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from services import draft_end

from conftest import seed_session

SID = "end-1"
LOG = {
    "sessionID": "DBABC123",
    "time": 1700000000000,
    "users": {
        "u1": {"userName": "Alice", "seatNum": 0, "picks": []},
        "u2": {"userName": "Bob", "seatNum": 1, "picks": []},
    },
}


async def _seed(**kw):
    await seed_session(session_id=SID, guild="1", stype="random", stage="teams",
                       draft_channel_id="999", sign_ups={"d1": "Alice", "d2": "Bob"},
                       **kw)


async def _row():
    async with AsyncSessionLocal() as s:
        return await s.scalar(select(DraftSession).filter_by(session_id=SID))


def _bot():
    channel = MagicMock()
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.get_guild.return_value = MagicMock(id=1)
    bot.get_channel.return_value = channel
    return bot, channel


def _quiet_log_steps():
    """Stub out everything after the rooms, so a test of the rooms sees only them."""
    return (patch("services.draft_end.capture_draft_log", AsyncMock(return_value=True)),
            patch("services.draft_end.post_team_logs", AsyncMock()),
            patch("services.draft_end.assign_drafted_decks", AsyncMock()))


@pytest.mark.asyncio
async def test_finish_draft_creates_rooms_once_when_called_twice(test_db):
    await _seed()
    bot, _ = _bot()

    async def slow_create(*a, **k):
        await asyncio.sleep(0.05)   # both callers are in flight at once
        return True

    create = AsyncMock(side_effect=slow_create)
    cap, team, decks = _quiet_log_steps()
    with patch.object(views.PersistentView, "create_rooms_pairings", create), \
         cap as capture, team, decks:
        await asyncio.gather(draft_end.finish_draft(bot, SID, LOG),
                             draft_end.finish_draft(bot, SID, LOG))

    create.assert_awaited_once()
    # The loser of the claim still carried on with the log.
    assert capture.await_count == 2


@pytest.mark.asyncio
async def test_finish_draft_with_no_log_still_creates_rooms(test_db):
    await _seed()
    bot, channel = _bot()
    create = AsyncMock(return_value=True)
    cap, team, decks = _quiet_log_steps()
    with patch.object(views.PersistentView, "create_rooms_pairings", create), \
         cap as capture, team as team_logs, decks as assign:
        await draft_end.finish_draft(bot, SID, None)

    create.assert_awaited_once()
    capture.assert_not_awaited()
    team_logs.assert_not_awaited()
    assign.assert_not_awaited()
    channel.send.assert_awaited_once_with("Rooms and Pairings have been created!")


@pytest.mark.asyncio
async def test_finish_draft_with_a_log_posts_pools_and_assigns_decks(test_db):
    await _seed()
    bot, _ = _bot()
    cap, team, decks = _quiet_log_steps()
    with patch.object(views.PersistentView, "create_rooms_pairings", AsyncMock(return_value=True)), \
         cap as capture, team as team_logs, decks as assign:
        await draft_end.finish_draft(bot, SID, LOG)

    capture.assert_awaited_once_with(SID, LOG)
    team_logs.assert_awaited_once_with(SID, bot)
    assign.assert_awaited_once_with(SID)


@pytest.mark.asyncio
async def test_a_failed_room_creation_gives_the_claim_back(test_db):
    """A run that built nothing must not leave the draft looking built: the
    "Create Rooms & Post Pairings" button and /mutiny both read rooms_created_at
    as "done", and would refuse the retry the failure message tells people to make."""
    await _seed()
    bot, channel = _bot()
    cap, team, decks = _quiet_log_steps()
    with patch.object(views.PersistentView, "create_rooms_pairings", AsyncMock(return_value=False)), \
         cap, team, decks:
        await draft_end.finish_draft(bot, SID, None)

    assert (await _row()).rooms_created_at is None
    channel.send.assert_awaited_once_with(
        "Failed to create rooms and pairings. Check logs for details.")


@pytest.mark.asyncio
async def test_rooms_that_already_exist_are_not_claimed_or_announced(test_db):
    built = datetime(2026, 1, 1, 12, 0)
    await _seed(rooms_created_at=built)
    bot, channel = _bot()
    create = AsyncMock(return_value=True)
    cap, team, decks = _quiet_log_steps()
    with patch.object(views.PersistentView, "create_rooms_pairings", create), \
         cap as capture, team, decks:
        await draft_end.finish_draft(bot, SID, LOG)

    create.assert_not_awaited()
    channel.send.assert_not_awaited()
    capture.assert_awaited_once_with(SID, LOG)
    assert (await _row()).rooms_created_at == built


@pytest.mark.asyncio
async def test_capture_draft_log_stores_the_log_under_the_session(test_db):
    await _seed()
    with patch("services.draft_end.upload_draft_log",
               AsyncMock(return_value="team/x.json")) as upload:
        ok = await draft_end.capture_draft_log(SID, LOG)

    assert ok is True
    upload.assert_awaited_once()
    row = await _row()
    async with AsyncSessionLocal() as s:
        stored = (await s.execute(
            select(DraftSession.draft_data).filter_by(session_id=SID))).scalar_one()
    assert stored == LOG
    assert row.spaces_object_key == "team/x.json"
    assert row.logs_captured_at is not None
    assert row.unlock_at == row.logs_captured_at + timedelta(
        seconds=draft_end.PUBLISH_DELAY_SECONDS)
    assert set(row.pack_first_picks) == {"d1", "d2"}
    assert not row.data_received   # captured, not published


@pytest.mark.asyncio
async def test_upload_files_the_log_by_session_type_and_cube():
    result = SimpleNamespace(success=True, object_path="swiss/Cube-1-DB1.json")
    helper = MagicMock()
    helper.upload_json = AsyncMock(return_value=result)
    with patch("services.draft_end.DigitalOceanHelper", return_value=helper):
        key = await draft_end.upload_draft_log("swiss", "Cube", {"time": 1, "sessionID": "DB1"})
    assert key == "swiss/Cube-1-DB1.json"
    helper.upload_json.assert_awaited_once_with(
        {"time": 1, "sessionID": "DB1"}, "swiss", "Cube-1-DB1.json")


# --- the claim against the real create_rooms_pairings ----------------------
#
# Everything above mocks create_rooms_pairings, which is exactly what would hide
# the claim tripping the function's own "already created" check: it reads the
# same column the claim writes.

@pytest.mark.asyncio
async def test_the_claimant_builds_the_draft_and_nobody_else_does(monkeypatch, test_db):
    from test_rooms_cannot_strand import (RACE_ID, _Chan, _seed_race_draft,
                                          _stub_everything_but_the_race)
    await _seed_race_draft()
    posted = _stub_everything_but_the_race(monkeypatch)
    guild = SimpleNamespace(id=1, get_member=lambda uid: SimpleNamespace(
        id=uid, display_name=str(uid)), get_channel=lambda cid: _Chan())
    bot = SimpleNamespace(get_channel=lambda cid: None)

    claim = await draft_end.claim_rooms(RACE_ID)
    assert claim is not None
    assert await draft_end.claim_rooms(RACE_ID) is None, "a claim was granted twice"

    # The button, pressed while the claimant is building, is turned away...
    assert await views.PersistentView.create_rooms_pairings(bot, guild, RACE_ID) is False
    # ...and the claimant is not stopped by its own claim.
    assert await views.PersistentView.create_rooms_pairings(
        bot, guild, RACE_ID, claim=claim) is True
    assert posted.await_count == 1
