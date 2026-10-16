"""What a donation draft's embeds say about its money."""
import pytest
from sqlalchemy import update

from database.db_session import db_session
from models.stake import StakeInfo

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


def test_donation_drafts_do_not_call_their_stakes_a_prize_pool():
    from services.donation_service import (SIGNUP_DONATION_NOTE, payouts_footer,
                                           payouts_title, stakes_field_name)

    assert stakes_field_name(True, 200) == "**Donation stakes: 200 tix**"
    assert stakes_field_name(False, 200) == "**Prize Pool: 200 tix**"
    assert payouts_title(True) == "💜 Donations to DraftBot"
    assert payouts_title(False) == "💰 Prize Pool Payouts"
    assert payouts_footer(True, 100) == "Donated to DraftBot: 100 tix"
    assert payouts_footer(False, 100) == "Winnings paid: 100 tix"
    assert "Nothing leaves your wallet" in SIGNUP_DONATION_NOTE
    assert "/donation_day paid_cash" in SIGNUP_DONATION_NOTE


@pytest.mark.asyncio
async def test_players_no_longer_on_a_team_are_not_listed_or_counted(test_db):
    """Removed after teams formed: their recorded share stays, but is not shown."""
    await seed_session("d1", guild="g", stage="teams", donation=True,
                       sign_ups=dict(NAMES), teams=(["a1", "a2"], ["b1", "b2"]))
    await seed_stakes("d1", {"a1": (50, False), "a2": (50, False),
                             "b1": (50, False), "b2": (50, False),
                             "gone": (50, False)})
    await level_donation("d1", *TEAMS)
    async with db_session() as s:  # the removed player's share, recorded earlier
        await s.execute(update(StakeInfo).where(StakeInfo.player_id == "gone")
                        .values(assigned_stake=50))

    lines, total = await get_formatted_stake_pairs("d1", NAMES)
    assert len(lines) == 4 and total == 200
    assert not any("Unknown" in line for line in lines)

    out, donated = await get_formatted_bet_outcomes("d1", NAMES, ["a1", "a2"])
    assert len(out) == 4 and donated == 100
    assert not any("Unknown" in line for line in out)


@pytest.mark.asyncio
async def test_a_corrected_result_does_not_change_what_was_booked(test_db):
    """The ledger keeps the first decision; the victory embed must say the same."""
    from unittest.mock import AsyncMock, patch

    import services.donation_service as donation
    from utils import settle_decided_draft

    teams = (["101", "102"], ["201", "202"])
    names = {"101": "Ann", "102": "Ben", "201": "Cat", "202": "Dan"}
    await seed_session("d1", guild="g", stage="pairings", donation=True, sign_ups=names,
                       teams=teams, matches=[("101", "201", "101", None)], match_counter=2)
    await seed_stakes("d1", {"101": (100, False), "102": (50, False),
                             "201": (50, False), "202": (50, False)})
    await level_donation("d1", *teams)
    with patch.object(donation, "draftbot_id", return_value="999"), \
         patch("notification_service.notify_wallet", AsyncMock()):
        await settle_decided_draft("d1")

    lines, total = await get_formatted_bet_outcomes("d1", names, teams[1])

    assert "**Ann** won 50 tix for DraftBot" in lines
    assert "**Cat** donates 50 tix to DraftBot" in lines
    assert not any("**Ann** donates" in line or "**Cat** won" in line for line in lines)
    assert total == 100
