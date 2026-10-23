from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import discord
import pytest

from cogs import lfg_cog

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_flag_off_has_no_link_and_no_view():
    with patch.object(lfg_cog, "scheduler_enabled", return_value=False), \
            patch.object(lfg_cog, "issue_link", new=AsyncMock()) as issue:
        content, view = await lfg_cog.lfg_reply("1", "2", NOW)
    assert content == "Find a team isn't turned on in this server."
    assert view is None
    issue.assert_not_called()


@pytest.mark.asyncio
async def test_flag_on_returns_link_button():
    url = "http://localhost:8787/#tok"
    with patch.object(lfg_cog, "scheduler_enabled", return_value=True), \
            patch.object(lfg_cog, "issue_link", new=AsyncMock(return_value=url)) as issue:
        content, view = await lfg_cog.lfg_reply("1", "2", NOW)
    issue.assert_awaited_once_with("2", "1", "lfg", NOW)
    assert "The link works for 7 days." in content
    assert view is not None
    button = view.children[0]
    assert button.label == "Open Find a team"
    assert button.url == url


@pytest.mark.asyncio
async def test_link_failure_gives_retry_text():
    with patch.object(lfg_cog, "scheduler_enabled", return_value=True), \
            patch.object(lfg_cog, "issue_link", new=AsyncMock(side_effect=RuntimeError("db"))):
        content, view = await lfg_cog.lfg_reply("1", "2", NOW)
    assert content == "Couldn't make your link just now. Try again in a minute."
    assert view is None


@pytest.mark.asyncio
async def test_board_view_is_persistent():
    view = lfg_cog.LfgBoardView()
    assert view.timeout is None
    button = view.children[0]
    assert button.custom_id == "lfg:open"
    assert button.label == "Find a team"
    assert button.style == discord.ButtonStyle.success
    assert view.is_persistent()
