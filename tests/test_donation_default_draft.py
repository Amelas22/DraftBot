"""/draft asks nothing: the server's donation day decides which kind of draft it opens."""
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.draft_commands import DraftCommands
from services.donation_day import draft_kind


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
    (True, {"active": True}, ("staked view", True)),
    (True, {"active": False}, ("staked view", False)),
    (True, None, ("staked view", False)),
    (False, {"active": True}, ("free view", None)),
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
