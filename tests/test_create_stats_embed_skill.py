"""create_stats_embed renders a 🎯 Skill Rating field from injected skill keys."""
import pytest
from conftest import StubUser, embed_field, stats_dict

from player_stats import create_stats_embed

SCALE_NOTE = "\n*New players start at 1500 · a 100-point gap ≈ 60% match favorite*"


@pytest.mark.asyncio
async def test_established_rating_shown_plain():
    lifetime = stats_dict(skill_rating=1620, skill_provisional=False)
    embed = await create_stats_embed(StubUser(), stats_dict(), stats_dict(), lifetime)
    assert embed_field(embed, "🎯 Skill Rating").value == "1620" + SCALE_NOTE


@pytest.mark.asyncio
async def test_provisional_rating_labelled():
    lifetime = stats_dict(skill_rating=1552, skill_provisional=True)
    embed = await create_stats_embed(StubUser(), stats_dict(), stats_dict(), lifetime)
    assert embed_field(embed, "🎯 Skill Rating").value == "1552 (provisional)" + SCALE_NOTE


@pytest.mark.asyncio
async def test_no_field_when_unrated():
    embed = await create_stats_embed(StubUser(), stats_dict(), stats_dict(), stats_dict())
    assert embed_field(embed, "🎯 Skill Rating") is None
