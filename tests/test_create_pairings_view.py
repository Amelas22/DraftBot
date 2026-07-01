"""create_pairings_view colors buttons by stored result."""
import discord
import pytest

from views import create_pairings_view


class _MR:
    def __init__(self, id, match_number, winner_id):
        self.id = id
        self.match_number = match_number
        self.winner_id = winner_id


@pytest.mark.asyncio
async def test_buttons_colored_by_result():
    match_results = [
        _MR(1, 1, "2"),    # team A winner -> red
        _MR(2, 2, "5"),    # team B winner -> blurple
        _MR(3, 3, None),   # unreported -> grey
    ]
    view = await create_pairings_view(
        bot=None, guild=None, session_id="s", match_results=match_results,
        team_a=["1", "2", "3"], team_b=["4", "5", "6"],
    )
    styles = [child.style for child in view.children]
    assert styles == [
        discord.ButtonStyle.danger,
        discord.ButtonStyle.primary,
        discord.ButtonStyle.secondary,
    ]


@pytest.mark.asyncio
async def test_defaults_to_grey_without_teams():
    view = await create_pairings_view(
        bot=None, guild=None, session_id="s",
        match_results=[_MR(1, 1, "2")],
    )
    assert view.children[0].style == discord.ButtonStyle.secondary
