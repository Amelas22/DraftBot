"""The hosted poller's edges: which drafts it watches, and how it rides out a
failed start, a refused token, a server known to be down, and a re-sync of the
players mid-check."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger
from sqlalchemy import update

from database.db_session import AsyncSessionLocal
from helpers.hosted_seats import seat_list
from models.draft_session import DraftSession
from services import draft_messages, hosted_draft
from services.hosted_draftmancer import HostedRefused, HostedUnavailable

from conftest import seed_session
from hosted_fakes import (  # noqa: F401 -- fixtures are used by name
    DRAFT_ID, SID, SIGN_UPS, _row, _seed, bot, channel, dms, everyone, hosted,
)


def _statuses(hosted) -> int:
    calls = {"n": 0}
    status = hosted.status

    async def counting(draft_id: str):
        calls["n"] += 1
        return await status(draft_id)

    hosted.status = counting
    return calls  # type: ignore[return-value]


# --- what is polled ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_draft_opened_more_than_a_day_ago_is_not_polled(hosted, bot):
    await _seed(start=datetime.now() - timedelta(hours=25))
    calls = _statuses(hosted)
    await hosted_draft.poll_hosted_drafts(bot)
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_an_abandoned_draft_is_not_polled(hosted, bot):
    await _seed(stage="abandoned")
    calls = _statuses(hosted)
    await hosted_draft.poll_hosted_drafts(bot)
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_a_live_draft_whose_rooms_were_built_early_is_polled_until_captured(hosted, bot, channel):
    await _seed(rooms_created_at=datetime.now())
    hosted.stage = "drafting"
    calls = _statuses(hosted)
    await hosted_draft.poll_hosted_drafts(bot)
    assert calls["n"] == 1

    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == SID)
                        .values(logs_captured_at=datetime.now()))
        await s.commit()
    await hosted_draft.poll_hosted_drafts(bot)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_an_old_draft_hears_of_no_outage(hosted, bot, channel):
    await _seed(start=datetime.now() - timedelta(days=3))
    hosted_draft._observe_api("unanswered")
    hosted.clock["now"] += hosted_draft.OUTAGE_ALERT_AFTER_SECONDS
    hosted.fail("ping", HostedUnavailable("down"))
    with patch("services.hosted_draft.send_dm", AsyncMock(return_value=True)):
        await hosted_draft.poll_hosted_drafts(bot)
    assert channel.sent("isn't answering") == []


# --- a refused token ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_refused_status_is_a_warning_not_a_traceback(hosted, bot):
    await _seed()
    hosted.fail("status", HostedRefused(401, {"error": "bad token"}))
    records: list[str] = []
    sink = logger.add(lambda m: records.append(m.record["level"].name), level="WARNING")
    try:
        await hosted_draft.poll_hosted_drafts(bot)
    finally:
        logger.remove(sink)
    assert "WARNING" in records and "ERROR" not in records
    assert SID not in hosted_draft._DONE   # polled again once the token is fixed


# --- sign-ups while the server is known down --------------------------------------------

@pytest.mark.asyncio
async def test_a_sign_up_does_not_wait_on_a_server_known_to_be_down(hosted):
    await _seed(stage=None)
    hosted_draft._observe_api("unanswered")
    hosted.clock["now"] += hosted_draft.OUTAGE_ALERT_AFTER_SECONDS
    await hosted_draft.on_signups_changed(SID)
    assert hosted.calls == []


# --- a start that does not reach the server -------------------------------------------

@pytest.mark.asyncio
async def test_a_start_the_server_missed_is_retried_without_a_new_ready_check(hosted, bot, channel, dms):
    await _seed()
    hosted.seats = everyone()
    await hosted_draft.poll_hosted_drafts(bot)          # the check opens
    hosted.seats = everyone(ready="ready")
    start = hosted.start
    hosted.fail("start", HostedUnavailable("down"))
    await hosted_draft.poll_hosted_drafts(bot)          # passes; the start fails
    assert len(channel.sent("Draft starting in 5 seconds")) == 1

    hosted.start = start                                 # type: ignore[method-assign]
    await hosted_draft.poll_hosted_drafts(bot)          # retried quietly
    assert hosted.call_names().count("ready_check") == 1
    assert hosted.calls[-2:] == [("start", "raised"), ("start", DRAFT_ID)]
    assert len(channel.sent("Draft starting in 5 seconds")) == 1
    assert hosted.stage == "drafting"


@pytest.mark.asyncio
async def test_a_missed_start_with_a_seat_no_longer_ready_runs_a_new_check(hosted, bot, channel, dms):
    await _seed()
    hosted.seats = everyone()
    await hosted_draft.poll_hosted_drafts(bot)
    hosted.seats = everyone(ready="ready")
    hosted.fail("start", HostedUnavailable("down"))
    await hosted_draft.poll_hosted_drafts(bot)

    hosted.seats = everyone(ready="ready", Dan=(True, "unknown"))
    await hosted_draft.poll_hosted_drafts(bot)
    assert hosted.call_names().count("ready_check") == 2


# --- a re-sync mid-check ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_re_syncing_the_players_says_why_the_running_check_ended(hosted, bot, channel, dms):
    await _seed()
    hosted.seats = everyone()
    await hosted_draft.poll_hosted_drafts(bot)
    [count] = channel.sent("Readycheck in progress")

    hosted.seats = everyone()[:3]       # the server lost Dan's seat
    await hosted_draft.poll_hosted_drafts(bot)
    assert "Readycheck in progress" not in count.content
    assert "Ready check failed" in count.content and "Dan" in count.content


# --- route recorded or the room closed ---------------------------------------------------

@pytest.mark.asyncio
async def test_a_route_that_cannot_be_recorded_closes_the_room_and_falls_back(hosted, monkeypatch):
    await _seed(stage=None, route=None)
    store = hosted_draft._store

    async def failing_store(session_id: str, **values):
        if values.get("draftmancer_route") == hosted_draft.ROUTE:
            raise RuntimeError("database is locked")
        await store(session_id, **values)

    monkeypatch.setattr(hosted_draft, "_store", failing_store)
    assert await hosted_draft.open_draft_room(await _row(), channel=None) == "fallback"
    assert hosted.call_names() == ["create", "stop"]
    row = await _row()
    assert row.draftmancer_route == "fallback"
    assert row.draft_link.startswith("https://draftmancer.com/")


# --- staked seating ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_staked_draft_is_seated_in_the_drawn_order(hosted):
    import services.team_creator as team_creator
    from session import get_draft_session

    players = {f"p{i}": f"Player {i}" for i in range(8)}
    await seed_session(SID, guild="1", stype="staked", stage=None, sign_ups=dict(players),
                       draft_id=DRAFT_ID, start=datetime.now(),
                       deletion_time=datetime.now() + timedelta(minutes=90))
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == SID)
                        .values(draftmancer_route="hosted"))
        await s.commit()

    interaction = MagicMock()
    interaction.guild_id = 1
    interaction.message = SimpleNamespace(id=0)
    interaction.followup.send = AsyncMock()
    interaction.followup.edit_message = AsyncMock()
    interaction.channel.send = AsyncMock()
    view = MagicMock(session_type="staked", children=[])
    with patch.object(team_creator, "match_pool", AsyncMock(return_value={})), \
         patch.object(team_creator, "_create_teams_embed", AsyncMock()), \
         patch.object(team_creator, "_create_channel_announcement_embed", AsyncMock()), \
         patch.object(team_creator, "send_teams_created_dms", AsyncMock()), \
         patch.object(team_creator, "_update_draft_manager", AsyncMock()), \
         patch.object(team_creator, "check_weekly_limits", AsyncMock()), \
         patch("services.entry_notices.announce_refunds", AsyncMock()), \
         patch("ready_check.ReadyCheckSession.cleanup", AsyncMock()):
        assert await team_creator.create_and_display_teams(MagicMock(), SID, interaction, view)

    stored = (await get_draft_session(SID)).sign_ups
    [seating] = [c[2] for c in hosted.calls if c[0] == "set_seating"]
    assert seating == seat_list(stored)


# --- wording -----------------------------------------------------------------------------

def test_the_status_message_names_the_ready_check_timeout_it_runs():
    text = draft_messages.format_status_message(draft_messages.session_status(["A"], []))
    assert f"time out after {draft_messages.READY_CHECK_TIMEOUT_SECONDS} seconds" in text


def test_a_timed_out_check_promises_only_what_happens():
    text = draft_messages.ready_check_timed_out_text(90, ["Dan"])
    assert "when all players are present" not in text
    assert "/ready" in text


def test_the_outage_notice_does_not_reserve_mutiny_for_admins():
    assert "an admin can" not in hosted_draft.OUTAGE_NOTICE
    assert "/mutiny" in hosted_draft.OUTAGE_NOTICE
