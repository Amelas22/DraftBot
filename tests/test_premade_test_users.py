"""Tests for TEST_MODE: filling both teams of a premade draft with test users."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers.test_users import plan_premade_test_users


# ---- plan_premade_test_users (pure) -----------------------------------------------

def test_fills_both_empty_teams_to_target():
    new_users, team_a, team_b = plan_premade_test_users([], [], "Alpha", "Bravo")
    assert len(team_a) == 3 and len(team_b) == 3
    assert len(new_users) == 6
    assert set(team_a + team_b) == set(new_users.keys())
    assert len(set(new_users.keys())) == 6  # unique ids


def test_tops_up_partially_filled_teams():
    existing_a = ["111", "222"]
    new_users, team_a, team_b = plan_premade_test_users(existing_a, ["333"], "Alpha", "Bravo")
    assert team_a[:2] == ["111", "222"]  # existing players untouched, in order
    assert team_b[0] == "333"
    assert len(team_a) == 3 and len(team_b) == 3
    assert len(new_users) == 3  # 1 for A + 2 for B
    assert "111" not in new_users  # real players don't get renamed


def test_noop_when_both_teams_full():
    full_a = ["1", "2", "3"]
    full_b = ["4", "5", "6"]
    new_users, team_a, team_b = plan_premade_test_users(full_a, full_b, "Alpha", "Bravo")
    assert new_users == {}
    assert team_a == full_a and team_b == full_b


def test_names_are_marked_and_team_labelled():
    new_users, team_a, team_b = plan_premade_test_users([], [], "Alpha", "Bravo")
    for uid in team_a:
        assert "[TEST]" in new_users[uid] and "Alpha" in new_users[uid]
    for uid in team_b:
        assert "[TEST]" in new_users[uid] and "Bravo" in new_users[uid]


def test_ids_do_not_collide_with_existing_test_users():
    first, team_a, team_b = plan_premade_test_users([], [], "Alpha", "Bravo")
    # Simulate a second click after one more real player joins team A
    team_a_after = team_a + []
    second, _, _ = plan_premade_test_users(team_a_after[:2], team_b, "Alpha", "Bravo",
                                           existing_ids=set(first.keys()))
    assert not (set(second.keys()) & set(first.keys()))


# ---- generate_seating_order falls back for test users ---------------------------------

@pytest.mark.asyncio
async def test_seating_order_uses_sign_up_names_for_non_members():
    import utils

    draft_session = MagicMock()
    draft_session.guild_id = "1"
    draft_session.team_a = ["111", "900000000000000001"]
    draft_session.team_b = ["222", "900000000000000002"]
    draft_session.sign_ups = {
        "111": "Real A", "222": "Real B",
        "900000000000000001": "[TEST] Alpha User 2",
        "900000000000000002": "[TEST] Bravo User 2",
    }

    def get_member(uid):
        if uid in (111, 222):
            member = MagicMock()
            member.display_name = f"Member{uid}"
            return member
        return None  # test users are not guild members

    guild = MagicMock()
    guild.get_member.side_effect = get_member
    bot = MagicMock()
    bot.get_guild.return_value = guild

    with patch.object(utils, "get_display_name", lambda member, g: member.display_name):
        order = await utils.generate_seating_order(bot, draft_session)

    assert len(order) == 4, f"test users were dropped from seating: {order}"
    assert "[TEST] Alpha User 2" in order
    assert "[TEST] Bravo User 2" in order
    assert "Member111" in order and "Member222" in order
