"""A player is asked once whether they borrow from a rental library.

The answer is a saved preference per player and server. It is asked right after
sign-up, only while unset, and only where it can matter: a rental library behind
the server, offering the draft's cube, that the player may borrow from. Deck
assignment then offers a rental library's decks only to the players who said yes.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import a_library
from database.db_session import AsyncSessionLocal
from models.player import PlayerStats
import preference_service as prefs
import services.library_access_service as access
import services.library_prompt as prompt

pytestmark = pytest.mark.asyncio

LIB, GUILD, CUBE = "lounge", "g1", "mycube"
MEMBER, PLAYER = "1001", "1002"


def _interaction(player=PLAYER, guild=GUILD):
    return SimpleNamespace(
        user=SimpleNamespace(id=player), guild_id=guild,
        followup=SimpleNamespace(send=AsyncMock()),
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))


async def _lounge(kind="rental"):
    """Fifty from a member, a hundred from anybody with three drafts."""
    await a_library(LIB, guild=GUILD, kind=kind, collateral=50,
                    collateral_untrusted=100, cubes=(CUBE,), min_drafts=3)
    await access.invite(LIB, MEMBER, "test")


async def _drafted(player, n):
    async with AsyncSessionLocal() as session:
        session.add(PlayerStats(player_id=player, guild_id=GUILD,
                                display_name=player, drafts_participated=n))
        await session.commit()


# --- the saved answer -------------------------------------------------------

async def test_an_unasked_player_reads_as_none_not_false(test_db):
    """None is what makes the sign-up flow ask. A default would mean nobody
    ever is."""
    assert await prefs.get_uses_library(PLAYER, GUILD) is None


async def test_the_answer_is_saved_per_server(test_db):
    await prefs.set_uses_library(PLAYER, GUILD, True)

    assert await prefs.get_uses_library(PLAYER, GUILD) is True
    assert await prefs.get_uses_library(PLAYER, "g-other") is None


async def test_saving_it_leaves_the_cap_preference_alone(test_db):
    await prefs.update_player_bet_capping_preference(PLAYER, GUILD, False)
    await prefs.set_uses_library(PLAYER, GUILD, True)

    assert await prefs.get_player_bet_capping_preference(PLAYER, GUILD) is False


async def test_only_a_yes_counts_as_wanting_a_deck(test_db):
    await prefs.set_uses_library("1", GUILD, True)
    await prefs.set_uses_library("2", GUILD, False)

    assert await prefs.get_players_using_library(["1", "2", "3"], GUILD) == {"1"}


async def test_the_switch_turns_an_unasked_player_on_then_off(test_db):
    assert await prompt.toggle_uses_library(PLAYER, GUILD) is True
    assert await prompt.toggle_uses_library(PLAYER, GUILD) is False


# --- when it is asked -------------------------------------------------------

@pytest.mark.parametrize("player,hold", [(MEMBER, 50), (PLAYER, 100)])
async def test_an_unasked_eligible_player_is_asked_with_their_own_hold(
        test_db, player, hold):
    await _lounge()
    await _drafted(PLAYER, 3)
    interaction = _interaction(player)

    await prompt.maybe_ask_library_preference(interaction, CUBE)

    interaction.followup.send.assert_awaited_once()
    text = interaction.followup.send.await_args.args[0]
    assert f"**{hold} tix**" in text, text
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


async def test_somebody_who_has_answered_is_not_asked_again(test_db):
    await _lounge()
    await prefs.set_uses_library(MEMBER, GUILD, False)
    interaction = _interaction(MEMBER)

    await prompt.maybe_ask_library_preference(interaction, CUBE)

    interaction.followup.send.assert_not_awaited()


async def test_somebody_who_may_not_borrow_is_not_asked(test_db):
    await _lounge()
    await _drafted(PLAYER, 2)
    interaction = _interaction(PLAYER)

    await prompt.maybe_ask_library_preference(interaction, CUBE)

    interaction.followup.send.assert_not_awaited()


async def test_a_cube_the_library_does_not_offer_asks_nothing(test_db):
    await _lounge()
    interaction = _interaction(MEMBER)

    await prompt.maybe_ask_library_preference(interaction, "another-cube")

    interaction.followup.send.assert_not_awaited()


async def test_a_communal_library_asks_nothing(test_db):
    """It lends to everybody, as it always has; there is nothing to answer."""
    await _lounge(kind="communal")
    interaction = _interaction(MEMBER)

    await prompt.maybe_ask_library_preference(interaction, CUBE)

    interaction.followup.send.assert_not_awaited()


async def test_a_failure_never_reaches_the_sign_up(test_db, monkeypatch):
    """The player is already signed up; the prompt failing must not look like
    the sign-up did."""
    await _lounge()
    monkeypatch.setattr(prompt, "library_to_ask_about",
                        AsyncMock(side_effect=RuntimeError("db gone")))

    await prompt.maybe_ask_library_preference(_interaction(MEMBER), CUBE)


# --- answering it -----------------------------------------------------------

@pytest.mark.parametrize("answer", [True, False])
async def test_a_click_saves_the_answer_and_closes_the_prompt(test_db, answer):
    view = prompt.LibraryPreferenceView(PLAYER, GUILD)
    button = next(b for b in view.children if b.uses_library is answer)
    interaction = _interaction(PLAYER)

    await button.callback(interaction)

    assert await prefs.get_uses_library(PLAYER, GUILD) is answer
    assert interaction.response.edit_message.await_args.kwargs["view"] is None


async def test_ignoring_the_prompt_saves_nothing(test_db):
    """No click, no answer: asked again at the next sign-up."""
    view = prompt.LibraryPreferenceView(PLAYER, GUILD)
    await view.on_timeout()

    assert await prefs.get_uses_library(PLAYER, GUILD) is None
