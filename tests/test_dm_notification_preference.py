"""DM notifications follow the server's default until the player chooses.

player_preferences holds several unrelated answers in one row. Saving any of
the others creates that row, and it must not turn a player's DMs off: only the
toggle is a choice about DMs.
"""
from unittest.mock import patch

import pytest

import preference_service as prefs

pytestmark = pytest.mark.asyncio

GUILD, PLAYER = "g1", "1002"


@pytest.fixture(autouse=True)
def dms_on_by_default():
    with patch.object(prefs, "get_dm_notifications_default", return_value=True):
        yield


async def _both_reads(player=PLAYER):
    return (await prefs.get_player_dm_notification_preference(player, GUILD),
            (await prefs.get_players_dm_notification_preferences([player], GUILD))[player])


async def test_a_player_with_no_row_gets_the_server_default(test_db):
    assert await _both_reads() == (True, True)


async def test_answering_the_library_question_leaves_dms_alone(test_db):
    await prefs.set_uses_library(PLAYER, GUILD, False)
    assert await _both_reads() == (True, True)


async def test_saving_the_entry_cap_leaves_dms_alone(test_db):
    await prefs.update_player_bet_capping_preference(PLAYER, GUILD, False)
    assert await _both_reads() == (True, True)


async def test_turning_dms_off_is_kept_over_the_default(test_db):
    await prefs.set_uses_library(PLAYER, GUILD, True)
    await prefs.update_player_dm_notification_preference(PLAYER, GUILD, False)
    assert await _both_reads() == (False, False)


async def test_one_batch_holds_both_a_choice_and_an_unset_row(test_db):
    """Read together, so an unset row cannot cost the other player their choice."""
    chooser = "1003"
    await prefs.update_player_dm_notification_preference(chooser, GUILD, False)
    await prefs.set_uses_library(PLAYER, GUILD, True)
    assert await prefs.get_players_dm_notification_preferences([chooser, PLAYER], GUILD) == {
        chooser: False, PLAYER: True}


async def test_a_row_without_a_choice_follows_a_server_that_defaults_off(test_db):
    await prefs.set_uses_library(PLAYER, GUILD, True)
    with patch.object(prefs, "get_dm_notifications_default", return_value=False):
        assert await _both_reads() == (False, False)


async def test_an_int_player_id_still_finds_the_stored_choice(test_db):
    await prefs.update_player_dm_notification_preference(PLAYER, GUILD, False)
    assert await prefs.get_player_dm_notification_preference(int(PLAYER), GUILD) is False
