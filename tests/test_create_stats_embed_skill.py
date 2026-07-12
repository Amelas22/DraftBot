"""create_stats_embed renders a 🎯 Skill Rating field from injected skill keys."""
import pytest

from player_stats import create_stats_embed


def _stats(**overrides):
    base = {
        "display_name": "P", "drafts_played": 12, "matches_won": 5, "matches_played": 9,
        "match_win_percentage": 55.0, "trophies_won": 1,
        "team_drafts_played": 4, "team_drafts_won": 2, "team_drafts_tied": 0,
        "team_draft_win_percentage": 50.0,
        "current_win_streak": 0, "longest_win_streak": 3,
        "current_perfect_streak": 0, "longest_perfect_streak": 1,
        "cube_stats": {},
    }
    base.update(overrides)
    return base


class _User:
    display_name = "P"


def _field(embed, name):
    return next((f for f in embed.fields if f.name == name), None)


@pytest.mark.asyncio
async def test_established_rating_shown_plain():
    lifetime = _stats(skill_rating=1030, skill_provisional=False)
    embed = await create_stats_embed(_User(), _stats(), _stats(), lifetime)
    assert _field(embed, "🎯 Skill Rating").value == "1030"


@pytest.mark.asyncio
async def test_provisional_rating_labelled():
    lifetime = _stats(skill_rating=412, skill_provisional=True)
    embed = await create_stats_embed(_User(), _stats(), _stats(), lifetime)
    assert _field(embed, "🎯 Skill Rating").value == "412 (provisional)"


@pytest.mark.asyncio
async def test_no_field_when_unrated():
    embed = await create_stats_embed(_User(), _stats(), _stats(), _stats())
    assert _field(embed, "🎯 Skill Rating") is None
