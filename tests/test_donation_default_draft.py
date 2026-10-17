"""/draft asks nothing: the server's donation day decides which kind of draft it opens."""
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.draft_commands import DraftCommands
from datetime import datetime

from services.donation_day import donation_day_running, draft_kind


def test_a_running_donation_day_opens_a_donation_draft():
    assert draft_kind(money_server=True, event_active=True) == "donation"


def test_without_a_donation_day_a_money_server_opens_a_staked_draft():
    assert draft_kind(money_server=True, event_active=False) == "staked"


def test_a_free_server_never_opens_a_money_draft():
    assert draft_kind(money_server=False, event_active=True) is None
    assert draft_kind(money_server=False, event_active=False) is None


def test_draft_registers_no_options():
    """An option shows in every player's command picker, event or not. Read from the
    command as a loaded bot registers it, which is what reaches Discord."""
    import discord

    bot = discord.Bot()
    bot.add_cog(DraftCommands(bot))
    cmd = next(c for c in bot.pending_application_commands if c.name == "draft")
    assert cmd.to_dict().get("options") == []
    assert list(inspect.signature(DraftCommands.draft.callback).parameters) == ["self", "ctx"]


@pytest.mark.asyncio
@pytest.mark.parametrize("money_server,event,expect", [
    (True, {"active": True, "starts_at": "2000-01-01T00:00"}, ("staked view", True)),
    (True, {"active": True, "starts_at": "2999-01-01T00:00"}, ("staked view", False)),
    (True, {"active": False, "starts_at": "2000-01-01T00:00"}, ("staked view", False)),
    (True, None, ("staked view", False)),
    (False, {"active": True, "starts_at": "2000-01-01T00:00"}, ("free view", None)),
])
async def test_draft_opens_the_view_its_server_config_calls_for(monkeypatch, money_server, event, expect):
    import cogs.draft_commands as dc

    opened = []
    monkeypatch.setattr(dc, "is_money_server", lambda _g: money_server)
    monkeypatch.setattr(dc, "get_event", lambda _g: event)
    monkeypatch.setattr(dc, "StakedCubeDraftSelectionView",
                        lambda guild_id, donation: opened.append(("staked view", donation)) or MagicMock())
    monkeypatch.setattr(dc, "CubeDraftSelectionView",
                        lambda **kw: opened.append(("free view", None)) or MagicMock())
    ctx = MagicMock()
    ctx.response.send_message = AsyncMock()

    await DraftCommands.draft.callback(DraftCommands(MagicMock()), ctx)

    assert opened == [expect]


NOW = datetime(2026, 10, 20, 12, 0)


def test_a_donation_day_runs_once_it_has_started():
    event = {"active": True, "starts_at": "2026-10-20T11:00"}
    assert donation_day_running(event, NOW)


def test_a_donation_day_set_up_ahead_waits_for_its_start():
    event = {"active": True, "starts_at": "2026-10-20T18:00"}
    assert not donation_day_running(event, NOW)


def test_an_ended_or_absent_donation_day_is_not_running():
    assert not donation_day_running({"active": False, "starts_at": "2026-10-20T11:00"}, NOW)
    assert not donation_day_running(None, NOW)


def test_a_donation_day_starting_this_minute_is_running():
    """/donation_day setup with no start stores this minute, and must open at once."""
    assert donation_day_running({"active": True, "starts_at": "2026-10-20T12:00"}, NOW)


def test_an_unreadable_start_reads_as_not_running():
    """Only a hand-edited config gets here; a staked draft is the safe answer."""
    for bad in ("not a date", "2026-10-20T11:00+00:00"):
        assert not donation_day_running({"active": True, "starts_at": bad}, NOW)
