"""What a donation draft's embeds say about its money."""
import pytest

from conftest import seed_session, seed_stakes
from services.donation_service import level_donation
from utils import get_formatted_bet_outcomes, get_formatted_stake_pairs

NAMES = {"a1": "Ann", "a2": "Ben", "b1": "Cat", "b2": "Dan"}
TEAMS = (["a1", "a2"], ["b1", "b2"])


async def _levelled():
    await seed_session("d1", guild="g", stage="teams", donation=True,
                       sign_ups=dict(NAMES), teams=TEAMS)
    await seed_stakes("d1", {"a1": (100, False), "a2": (50, False),
                             "b1": (50, False), "b2": (50, False)})
    await level_donation("d1", *TEAMS)


@pytest.mark.asyncio
async def test_the_teams_embed_lists_each_recorded_share(test_db):
    await _levelled()

    lines, total = await get_formatted_stake_pairs("d1", NAMES)

    assert total == 200
    assert "**Ann**: 50 tix" in lines and len(lines) == 4


@pytest.mark.asyncio
async def test_the_victory_embed_says_who_raised_and_who_donates(test_db):
    await _levelled()

    lines, total = await get_formatted_bet_outcomes("d1", NAMES, TEAMS[0])

    assert total == 100
    assert "**Ann** won 50 tix for DraftBot" in lines
    assert "**Cat** donates 50 tix to DraftBot" in lines
