"""A player is asked once whether they borrow from a rental library.

The answer is a saved preference per player and server. It is asked right after
sign-up, only while unset, and only where it can matter: a rental library behind
the server, offering the draft's cube, that the player may borrow from. A
borrower who has answered is told instead whether their draft holds the
library's copy, or which queue does (services/library_reservation).
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


async def _lounge(kind="rental", stock=None):
    """Fifty from a member, a hundred from anybody with three drafts."""
    await a_library(LIB, guild=GUILD, kind=kind, collateral=50,
                    collateral_untrusted=100, cubes=(CUBE,), min_drafts=3,
                    stock=stock)
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

@pytest.fixture
def shelf(monkeypatch):
    """One copy of the cube, so a reserve attempt can be answered offline."""
    import services.card_library_inventory as inv

    async def fetch(cube_id):
        return [{"name": "Swamp", "qty": 4}] if cube_id == CUBE else None
    monkeypatch.setattr(inv, "fetch_cube", fetch)


async def _queue(players, sid="q1", cube=CUBE):
    from datetime import datetime, timedelta
    from models.draft_session import DraftSession
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id=sid, guild_id=GUILD, cube=cube,
                           sign_ups={p: p for p in players},
                           deletion_time=datetime.now() + timedelta(hours=3),
                           draft_channel_id="chan", message_id=f"msg-{sid}"))
        await s.commit()
    return SimpleNamespace(session_id=sid, cube=cube)


@pytest.mark.parametrize("player,hold", [(MEMBER, 50), (PLAYER, 100)])
async def test_an_unasked_eligible_player_is_asked_with_their_own_hold(
        test_db, shelf, player, hold):
    await _lounge()
    await _drafted(PLAYER, 3)
    interaction = _interaction(player)

    await prompt.library_after_sign_up(interaction, await _queue([player]))

    interaction.followup.send.assert_awaited_once()
    text = interaction.followup.send.await_args.args[0]
    assert f"**{hold} tix**" in text, text
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


async def test_a_borrower_is_not_asked_but_told_it_is_reserved(test_db, shelf):
    await _lounge(stock={"Swamp": 4})
    await prefs.set_uses_library(MEMBER, GUILD, True)
    interaction = _interaction(MEMBER)

    await prompt.library_after_sign_up(interaction, await _queue([MEMBER]))

    text = interaction.followup.send.await_args.args[0]
    assert "reserved for this draft" in text, text
    assert "view" in interaction.followup.send.await_args.kwargs, \
        "with the button to say they don't need it"


async def test_a_borrower_in_a_second_queue_is_pointed_at_the_first(test_db, shelf):
    await _lounge(stock={"Swamp": 4})
    await _drafted(PLAYER, 3)
    for p in (MEMBER, PLAYER):
        await prefs.set_uses_library(p, GUILD, True)
    await prompt.library_after_sign_up(_interaction(MEMBER), await _queue([MEMBER], "q1"))
    interaction = _interaction(PLAYER)

    await prompt.library_after_sign_up(interaction, await _queue([PLAYER], "q2"))

    text = interaction.followup.send.await_args.args[0]
    assert "in use" in text and "/chan/msg-q1" in text and "Join that one" in text, text


async def test_somebody_who_said_no_is_told_nothing(test_db, shelf):
    await _lounge()
    await prefs.set_uses_library(MEMBER, GUILD, False)
    interaction = _interaction(MEMBER)

    await prompt.library_after_sign_up(interaction, await _queue([MEMBER]))

    interaction.followup.send.assert_not_awaited()


async def test_somebody_who_may_not_borrow_is_not_asked(test_db, shelf):
    await _lounge()
    await _drafted(PLAYER, 2)
    interaction = _interaction(PLAYER)

    await prompt.library_after_sign_up(interaction, await _queue([PLAYER]))

    interaction.followup.send.assert_not_awaited()


async def test_a_cube_the_library_does_not_offer_asks_nothing(test_db, shelf):
    await _lounge()
    interaction = _interaction(MEMBER)

    await prompt.library_after_sign_up(
        interaction, await _queue([MEMBER], cube="another-cube"))

    interaction.followup.send.assert_not_awaited()


async def test_a_communal_library_asks_nothing(test_db, shelf):
    """It lends to everybody, as it always has; there is nothing to answer."""
    await _lounge(kind="communal")
    interaction = _interaction(MEMBER)

    await prompt.library_after_sign_up(interaction, await _queue([MEMBER]))

    interaction.followup.send.assert_not_awaited()


# --- answering it -----------------------------------------------------------

async def _view(player=PLAYER):
    from services.library_service import library_for
    draft = await _queue([player])
    return prompt.LibraryPreferenceView(player, GUILD, draft, await library_for(GUILD))


async def test_yes_saves_it_and_reserves_the_copy(test_db, shelf):
    await _lounge(stock={"Swamp": 4})
    await _drafted(PLAYER, 3)
    view = await _view()
    button = next(b for b in view.children if b.uses_library is True)
    interaction = _interaction(PLAYER)

    await button.callback(interaction)

    assert await prefs.get_uses_library(PLAYER, GUILD) is True
    text = interaction.response.edit_message.await_args.kwargs["content"]
    assert "reserved for this draft" in text, text


async def test_no_saves_it_and_closes_the_prompt(test_db, shelf):
    await _lounge()
    view = await _view()
    button = next(b for b in view.children if b.uses_library is False)
    interaction = _interaction(PLAYER)

    await button.callback(interaction)

    assert await prefs.get_uses_library(PLAYER, GUILD) is False
    assert interaction.response.edit_message.await_args.kwargs["view"] is None


async def test_ignoring_the_prompt_saves_nothing(test_db, shelf):
    """No click, no answer: asked again at the next sign-up."""
    await _lounge()
    view = await _view()
    await view.on_timeout()

    assert await prefs.get_uses_library(PLAYER, GUILD) is None


async def test_an_unanswered_prompt_closes_itself(test_db, shelf):
    await _lounge()
    view = await _view()
    view.sent = SimpleNamespace(edit=AsyncMock())

    await view.on_timeout()

    kwargs = view.sent.edit.await_args.kwargs
    assert kwargs["view"] is None and "/toggle_library" in kwargs["content"]


# --- when something goes wrong ----------------------------------------------

async def test_a_failed_read_does_not_flip_the_switch(test_db, monkeypatch):
    """Reading the answer as "not asked" on an error would turn a tap meant to
    switch it off into switching it on."""
    await prefs.set_uses_library(PLAYER, GUILD, True)
    monkeypatch.setattr(prompt, "get_uses_library",
                        AsyncMock(side_effect=RuntimeError("db gone")))

    assert await prompt.toggle_uses_library(PLAYER, GUILD) is None
    monkeypatch.undo()
    assert await prefs.get_uses_library(PLAYER, GUILD) is True


async def test_a_failure_says_how_to_opt_in_and_never_reaches_the_sign_up(
        test_db, shelf, monkeypatch):
    """The player is already signed up; and unanswered means no deck, so they
    are told the way round rather than left to find out at the end."""
    await _lounge()
    monkeypatch.setattr(prompt, "hold_for",
                        AsyncMock(side_effect=RuntimeError("db gone")))
    interaction = _interaction(MEMBER)

    await prompt.library_after_sign_up(interaction, await _queue([MEMBER]))

    said = interaction.followup.send.await_args.args[0]
    assert "/toggle_library" in said, said


async def test_a_server_with_no_library_is_not_told_anything(test_db):
    interaction = _interaction(MEMBER)

    await prompt.library_after_sign_up(interaction, SimpleNamespace(session_id="x", cube=CUBE))

    interaction.followup.send.assert_not_awaited()
