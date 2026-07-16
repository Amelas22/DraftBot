"""Regression tests for premade signup-embed updates.

The premade queue embed fields are color-coded ("🔴 Goblins"). update_team_view
must (a) still find those fields to update when a player joins, and (b) keep the
color emoji when it rewrites them — otherwise the queue freezes at "No players
yet." and/or loses the 🔴/🔵 key that add_sub's Red/Blue option relies on.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from views import PersistentView
from helpers.team_display import team_labels, TEAM_A_COLOR, TEAM_B_COLOR


def _premade_queue_embed():
    """An embed shaped like PremadeSession._add_signup_fields produces."""
    embed = discord.Embed(title="Premade Team Draft Queue")
    a_label, b_label = team_labels("premade", "Goblins", "Elves")
    embed.add_field(name=a_label, value="No players yet.", inline=True)
    embed.add_field(name=b_label, value="No players yet.", inline=True)
    return embed


def _make_view():
    return PersistentView(
        bot=MagicMock(),
        draft_session_id="sess_123",
        session_type="premade",
        team_a_name="Goblins",
        team_b_name="Elves",
    )


@pytest.mark.asyncio
async def test_join_updates_color_prefixed_field_and_keeps_emoji():
    embed = _premade_queue_embed()
    message = MagicMock()
    message.embeds = [embed]
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)

    view = _make_view()
    view.bot.get_channel = MagicMock(return_value=channel)

    session = SimpleNamespace(
        draft_channel_id="500", message_id="600",
        team_a=["1"], team_b=[],
        team_a_name="Goblins", team_b_name="Elves",
        session_type="premade",
        sign_ups={"1": "PlayerOne"},
    )
    interaction = MagicMock()
    interaction.guild = MagicMock()

    with patch("views.get_draft_session", AsyncMock(return_value=session)), \
         patch("views.get_display_name_by_id", lambda uid, guild, fallback="": "PlayerOne"):
        await view.update_team_view(interaction)

    # Team A field (🔴 Goblins) must now list the joined player, not "No players yet."
    a_field = embed.fields[0]
    assert "PlayerOne" in a_field.value
    # ...and it must keep the 🔴 color key.
    assert a_field.name.startswith(TEAM_A_COLOR)
    assert "Goblins" in a_field.name
    # Team B unchanged, still color-keyed.
    assert embed.fields[1].name.startswith(TEAM_B_COLOR)

    message.edit.assert_awaited_once()
