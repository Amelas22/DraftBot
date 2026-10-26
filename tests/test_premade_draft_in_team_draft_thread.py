"""/premade_draft pre-fills its launcher inside a team draft thread."""
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


def team_draft_thread():
    thread = MagicMock(spec=discord.Thread)
    thread.id = 900
    return thread


@pytest.mark.asyncio
async def test_premade_draft_in_a_team_draft_thread_is_pre_named():
    from cogs.draft_commands import DraftCommands

    ctx = make_ctx(team_draft_thread())
    overrides = {"team_a_name": "Team Garruk", "team_b_name": "Ash, Bo", "team_draft_id": 3}
    with patch("cogs.draft_commands.db_session", fake_session), \
         patch("match_control_view.match_room_context", AsyncMock(return_value=None)), \
         patch("team_draft_view.team_draft_room_context",
               AsyncMock(return_value=(3, overrides, None))) as lookup, \
         patch("cogs.draft_commands.CubeDraftSelectionView") as view:
        await DraftCommands.premade_draft.callback(DraftCommands(MagicMock()), ctx)

    assert lookup.await_args.args[1] == 900
    assert view.call_args.kwargs["session_details_overrides"] == overrides
    assert ctx.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_premade_draft_in_a_team_draft_thread_is_refused_once_started():
    from cogs.draft_commands import DraftCommands

    ctx = make_ctx(team_draft_thread())
    block = "This matchup's draft has already started — join it here: https://x"
    with patch("cogs.draft_commands.db_session", fake_session), \
         patch("match_control_view.match_room_context", AsyncMock(return_value=None)), \
         patch("team_draft_view.team_draft_room_context",
               AsyncMock(return_value=(3, {}, block))), \
         patch("cogs.draft_commands.CubeDraftSelectionView") as view:
        await DraftCommands.premade_draft.callback(DraftCommands(MagicMock()), ctx)

    view.assert_not_called()
    assert ctx.response.send_message.call_args.args[0] == block
    assert ctx.response.send_message.call_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_a_raising_team_draft_lookup_still_yields_an_ordinary_draft():
    from cogs.draft_commands import DraftCommands

    ctx = make_ctx(team_draft_thread())
    with patch("cogs.draft_commands.db_session", fake_session), \
         patch("match_control_view.match_room_context", AsyncMock(return_value=None)), \
         patch("team_draft_view.team_draft_room_context",
               AsyncMock(side_effect=RuntimeError("db hiccup"))), \
         patch("cogs.draft_commands.CubeDraftSelectionView") as view:
        await DraftCommands.premade_draft.callback(DraftCommands(MagicMock()), ctx)

    assert view.call_args.kwargs["session_details_overrides"] is None
    assert ctx.response.send_message.call_args.args[0] == "Select a cube:"


@pytest.mark.asyncio
async def test_a_match_thread_never_asks_the_team_draft_lookup():
    from cogs.draft_commands import DraftCommands

    ctx = make_ctx(team_draft_thread())
    overrides = {"tournament_match_id": 7, "team_a_name": "Alpha", "team_b_name": "Bravo"}
    with patch("cogs.draft_commands.db_session", fake_session), \
         patch("match_control_view.match_room_context",
               AsyncMock(return_value=(7, overrides, None))), \
         patch("team_draft_view.team_draft_room_context", AsyncMock()) as lookup, \
         patch("cogs.draft_commands.CubeDraftSelectionView") as view:
        await DraftCommands.premade_draft.callback(DraftCommands(MagicMock()), ctx)

    lookup.assert_not_awaited()
    assert view.call_args.kwargs["session_details_overrides"] == overrides
