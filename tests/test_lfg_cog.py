from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import discord
import pytest

from cogs import lfg_cog
from services.team_finder import BoardTeam

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


@pytest.mark.asyncio
async def test_respond_without_guild_replies_ephemerally():
    interaction = AsyncMock()
    interaction.guild_id = None
    await lfg_cog._respond(interaction)
    interaction.response.send_message.assert_awaited_once_with(
        "Use this in a server.", ephemeral=True)


@pytest.mark.asyncio
async def test_reconcile_loop_survives_a_failing_guild():
    bot = AsyncMock()
    bot.guilds = [AsyncMock(id=1), AsyncMock(id=2)]
    cog = lfg_cog.LfgCog.__new__(lfg_cog.LfgCog)
    cog.bot = bot
    calls = []

    def enabled(guild_id):
        if guild_id == "1":
            raise RuntimeError("config")
        return True

    async def run(_bot, guild_id):
        calls.append(guild_id)

    with patch.object(lfg_cog, "scheduler_enabled", side_effect=enabled), \
            patch.object(lfg_cog, "run_reconcile", new=run):
        await lfg_cog.LfgCog.reconcile_loop.coro(cog)
    assert calls == ["2"]


def _team(n=1, others=(), size=3, note=""):
    return BoardTeam(team_id=str(n), captain_name=f"Cap{n}", others=tuple(others), size=size, note=note)


def test_the_board_says_when_no_team_is_open():
    text = lfg_cog.board_embed([]).description
    assert text.startswith(lfg_cog.BOARD_TEXT)
    assert text.endswith("No open teams yet. Press **Find a team** to open one.")


def test_the_board_lists_each_open_team_with_its_members_and_note():
    text = lfg_cog.board_embed([
        _team(1, others=("Ben",), note="Evenings ET"),
        _team(2, size=4),
    ]).description
    assert text == (
        f"{lfg_cog.BOARD_TEXT}\n\n**Open teams**\n"
        "**Cap1's team** · 2/3 · needs 1\nwith Ben\n*Evenings ET*\n\n"
        "**Cap2's team** · 1/4 · needs 3"
    )


def test_names_and_notes_cannot_format_or_ping():
    text = lfg_cog.board_embed([
        BoardTeam(team_id="1", captain_name="**evil**", others=("@everyone",), size=3, note="_x_ <@123>"),
    ]).description
    assert "\\*\\*evil\\*\\*'s team" in text
    assert "@everyone" not in text.replace("@​everyone", "")
    assert "\\_x\\_" in text


def test_a_board_too_long_for_discord_ends_with_a_count_of_the_rest():
    teams = [_team(n, others=("x" * 100,), note="y" * 200) for n in range(40)]
    text = lfg_cog.board_embed(teams).description
    assert len(text) <= 4096
    shown = text.count("'s team**")
    assert text.endswith(f"…and {40 - shown} more on the website.")
    assert 0 < shown < 40
