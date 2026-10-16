from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cogs.donation_day_commands import new_event

NOW = datetime(2026, 10, 16, 12, 0)


def test_a_new_event_starts_now_unless_told_otherwise():
    event = new_event("Donation Day", 500, None, NOW)

    assert event == {"name": "Donation Day", "goal_usd": 500,
                     "starts_at": "2026-10-16T12:00", "page_key": "donation-day",
                     "active": True}


def test_an_explicit_start_is_parsed():
    assert new_event("DD", 100, "2026-10-16 09:30", NOW)["starts_at"] == "2026-10-16T09:30"


def test_a_bad_start_is_refused():
    with pytest.raises(ValueError):
        new_event("DD", 100, "friday", NOW)


def test_a_goal_must_be_positive():
    with pytest.raises(ValueError):
        new_event("DD", 0, None, NOW)


def test_rerunning_setup_on_an_active_event_keeps_its_start():
    existing = new_event("DD", 100, "2026-10-16 09:30", NOW)

    event = new_event("DD renamed", 200, None, NOW, existing=existing)

    assert event["starts_at"] == "2026-10-16T09:30"
    assert event["name"] == "DD renamed" and event["goal_usd"] == 200


def test_an_explicit_start_overrides_the_running_events_start():
    existing = new_event("DD", 100, "2026-10-16 09:30", NOW)

    event = new_event("DD", 100, "2026-10-16 11:00", NOW, existing=existing)

    assert event["starts_at"] == "2026-10-16T11:00"


def test_an_ended_event_does_not_lend_its_start():
    existing = {**new_event("DD", 100, "2026-10-16 09:30", NOW), "active": False}

    assert new_event("DD", 100, None, NOW, existing=existing)["starts_at"] == "2026-10-16T12:00"


def _end_ctx():
    ctx = MagicMock()
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    ctx.guild.id = 1
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("url, stays_active", [(None, True), ("https://x", False)])
async def test_end_deactivates_only_after_a_successful_final_upload(url, stays_active):
    import cogs.donation_day_commands as cmds
    cog = cmds.DonationDayCommands(MagicMock())
    event = new_event("DD", 100, None, NOW)
    with patch.object(cmds, "get_event", return_value=event), \
         patch.object(cmds, "publish_event", AsyncMock(return_value=url)), \
         patch.object(cmds, "save_event") as save:
        ctx = _end_ctx()
        await cmds.DonationDayCommands.end_event.callback(cog, ctx)

    assert save.called is (not stays_active)
    message = ctx.followup.send.await_args.args[0]
    assert ("run `/donation_day end` again" in message) is stays_active


def test_an_overlong_event_name_is_refused():
    with pytest.raises(ValueError):
        new_event("x" * 101, 100, None, NOW)


@pytest.mark.asyncio
async def test_force_ends_the_event_even_when_the_final_upload_failed():
    import cogs.donation_day_commands as cmds
    cog = cmds.DonationDayCommands(MagicMock())
    with patch.object(cmds, "get_event", return_value=new_event("DD", 100, None, NOW)), \
         patch.object(cmds, "publish_event", AsyncMock(return_value=None)), \
         patch.object(cmds, "save_event") as save:
        ctx = _end_ctx()
        await cmds.DonationDayCommands.end_event.callback(cog, ctx, force=True)

    assert save.call_args.args[1]["active"] is False
    assert "FAILED" in ctx.followup.send.await_args.args[0]
