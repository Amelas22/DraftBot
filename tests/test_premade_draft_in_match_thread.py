"""/premade_draft pre-fills its launcher inside a tournament match thread."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest


def make_ctx(channel):
    ctx = MagicMock()
    ctx.channel = channel
    ctx.guild.id = 1
    ctx.response.send_message = AsyncMock()
    return ctx


@asynccontextmanager
async def fake_session():
    yield MagicMock()


@pytest.mark.asyncio
async def test_outside_a_thread_the_command_is_unchanged():
    from cogs.draft_commands import DraftCommands

    ctx = make_ctx(MagicMock(spec=discord.TextChannel))
    with patch("cogs.draft_commands.CubeDraftSelectionView") as view:
        await DraftCommands.premade_draft.callback(DraftCommands(MagicMock()), ctx)

    assert view.call_args.kwargs["session_details_overrides"] is None
    send_args, send_kwargs = ctx.response.send_message.call_args
    assert send_args[0] == "Select a cube:"
    assert send_kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_in_a_non_match_thread_the_command_is_unchanged():
    from cogs.draft_commands import DraftCommands

    thread = MagicMock(spec=discord.Thread)
    thread.id = 12345
    ctx = make_ctx(thread)
    with patch("cogs.draft_commands.db_session", fake_session), \
         patch("match_control_view.match_room_context", AsyncMock(return_value=None)), \
         patch("cogs.draft_commands.CubeDraftSelectionView") as view:
        await DraftCommands.premade_draft.callback(DraftCommands(MagicMock()), ctx)

    assert view.call_args.kwargs["session_details_overrides"] is None
    send_args, send_kwargs = ctx.response.send_message.call_args
    assert send_args[0] == "Select a cube:"
    assert send_kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_in_a_match_thread_the_launcher_is_pre_filled():
    from cogs.draft_commands import DraftCommands

    thread = MagicMock(spec=discord.Thread)
    thread.id = 900
    ctx = make_ctx(thread)
    overrides = {"tournament_match_id": 7, "team_a_name": "Alpha", "team_b_name": "Bravo"}
    with patch("cogs.draft_commands.db_session", fake_session), \
         patch("match_control_view.match_room_context",
               AsyncMock(return_value=(7, overrides, None))), \
         patch("cogs.draft_commands.CubeDraftSelectionView") as view:
        await DraftCommands.premade_draft.callback(DraftCommands(MagicMock()), ctx)

    # Both names present is what suppresses the modal's team-name inputs.
    assert view.call_args.kwargs["session_details_overrides"] == overrides


@pytest.mark.asyncio
async def test_a_blocked_match_never_opens_the_picker():
    from cogs.draft_commands import DraftCommands

    thread = MagicMock(spec=discord.Thread)
    thread.id = 900
    ctx = make_ctx(thread)
    with patch("cogs.draft_commands.db_session", fake_session), \
         patch("match_control_view.match_room_context",
               AsyncMock(return_value=(7, {}, "A draft for this match is already underway."))), \
         patch("cogs.draft_commands.CubeDraftSelectionView") as view:
        await DraftCommands.premade_draft.callback(DraftCommands(MagicMock()), ctx)

    view.assert_not_called()
    assert "already underway" in ctx.response.send_message.call_args.args[0]
    assert ctx.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_a_raising_lookup_still_yields_a_working_ordinary_draft():
    from cogs.draft_commands import DraftCommands

    thread = MagicMock(spec=discord.Thread)
    thread.id = 900
    ctx = make_ctx(thread)
    with patch("cogs.draft_commands.db_session", fake_session), \
         patch("match_control_view.match_room_context",
               AsyncMock(side_effect=RuntimeError("db hiccup"))), \
         patch("cogs.draft_commands.CubeDraftSelectionView") as view:
        await DraftCommands.premade_draft.callback(DraftCommands(MagicMock()), ctx)

    # The tournament lookup is an enhancement — its failure must never break
    # the ordinary draft it enhances. The user sees the fallback because the
    # modal asks for team names again (overrides is None).
    assert view.call_args.kwargs["session_details_overrides"] is None
    send_args, send_kwargs = ctx.response.send_message.call_args
    assert send_args[0] == "Select a cube:"
    assert send_kwargs["ephemeral"] is True
