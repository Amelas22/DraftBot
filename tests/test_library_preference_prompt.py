"""A player is asked once whether they borrow from a rental library.

The answer is a saved preference per player and server. It is asked right after
sign-up, only while unset, and only where it can matter: a rental library behind
the server, offering the draft's cube, that the player may borrow from. A
borrower who has answered is told instead whether their draft holds the
library's copy, or which queue does (services/library_reservation). A held
copy waits on their "I agree" before the draft fires; answering yes to the
question itself, which quotes the hold, is agreeing.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import CUBE, GUILD, MEMBER, PLAYER, _drafted, _labels, _lounge, _queue, _row
from database.db_session import AsyncSessionLocal
import preference_service as prefs
import services.library_prompt as prompt

pytestmark = pytest.mark.asyncio


def _interaction(player=PLAYER, guild=GUILD):
    return SimpleNamespace(
        user=SimpleNamespace(id=player), guild_id=guild,
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock(),
                                 defer=AsyncMock()))


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


# --- when it is asked -------------------------------------------------------

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
    assert "library/index.html" in text, "with a link to what the library holds"
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


async def test_a_borrower_is_not_asked_but_told_it_is_held_until_they_agree(
        test_db, shelf):
    await _lounge(stock={"Swamp": 4})
    await prefs.set_uses_library(MEMBER, GUILD, True)
    interaction = _interaction(MEMBER)

    await prompt.library_after_sign_up(interaction, await _queue([MEMBER]))

    text = interaction.followup.send.await_args.args[0]
    assert "held for this draft" in text and prompt.AGREE_LABEL in text, text
    assert _labels(interaction.followup.send.await_args.kwargs["view"]) == [
        prompt.AGREE_LABEL, "I don't need the library this draft"]


async def _held_note(player=MEMBER, sid="q1"):
    """A saved borrower signs up; returns the note's view."""
    await prefs.set_uses_library(player, GUILD, True)
    interaction = _interaction(player)
    await prompt.library_after_sign_up(interaction, await _queue([player], sid))
    return interaction.followup.send.await_args.kwargs["view"]


async def test_agreeing_from_the_note_keeps_the_copy(test_db, shelf):
    await _lounge(stock={"Swamp": 4})
    view = await _held_note()
    agree = next(b for b in view.children if b.label == prompt.AGREE_LABEL)
    interaction = _interaction(MEMBER)

    await agree.callback(interaction)

    sent = interaction.edit_original_response.await_args.kwargs
    assert "reserved for this draft" in sent["content"], sent["content"]
    assert _labels(sent["view"]) == ["I don't need the library this draft"]
    assert (await _row()).library_agreed == [MEMBER]


async def test_nobody_else_can_agree_for_you(test_db, shelf):
    await _lounge(stock={"Swamp": 4})
    view = await _held_note()
    agree = next(b for b in view.children if b.label == prompt.AGREE_LABEL)
    interaction = _interaction(PLAYER)

    await agree.callback(interaction)

    interaction.edit_original_response.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    assert not (await _row()).library_agreed


async def test_an_old_agree_button_after_switching_off_says_so(test_db, shelf):
    await _lounge(stock={"Swamp": 4})
    view = await _held_note()
    await prefs.set_uses_library(MEMBER, GUILD, False)
    agree = next(b for b in view.children if b.label == prompt.AGREE_LABEL)
    interaction = _interaction(MEMBER)

    await agree.callback(interaction)

    said = interaction.edit_original_response.await_args.kwargs["content"]
    assert "isn't holding a copy for you" in said, said


async def test_agreeing_once_the_draft_fired_is_too_late(test_db, shelf):
    from datetime import datetime
    from models.draft_session import DraftSession
    from sqlalchemy import update

    await _lounge(stock={"Swamp": 4})
    view = await _held_note()
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == "q1")
                        .values(teams_start_time=datetime.now()))
        await s.commit()
    agree = next(b for b in view.children if b.label == prompt.AGREE_LABEL)
    interaction = _interaction(MEMBER)

    await agree.callback(interaction)

    assert interaction.edit_original_response.await_args.kwargs["content"] == prompt.TOO_LATE
    assert not (await _row()).library_agreed


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
    sent = interaction.edit_original_response.await_args.kwargs
    assert "reserved for this draft" in sent["content"], sent["content"]
    assert (await _row()).library_agreed == [PLAYER], \
        "the question quoted the hold, so yes is agreeing"
    assert _labels(sent["view"]) == ["I don't need the library this draft"]


async def test_no_saves_it_and_closes_the_prompt(test_db, shelf):
    await _lounge()
    view = await _view()
    button = next(b for b in view.children if b.uses_library is False)
    interaction = _interaction(PLAYER)

    await button.callback(interaction)

    assert await prefs.get_uses_library(PLAYER, GUILD) is False
    assert interaction.edit_original_response.await_args.kwargs["view"] is None


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
    assert kwargs["view"] is None and "Change Entry / Settings" in kwargs["content"]


# --- the row on Change Entry / Settings -------------------------------------

def _panel_interaction(player=PLAYER):
    interaction = _interaction(player)
    interaction.followup = SimpleNamespace(send=AsyncMock())
    return interaction


async def _row_buttons(uses, player=PLAYER, sid="q1"):
    from discord.ui import View
    from services.library_service import library_for
    draft = await _queue([player], sid)
    status, borrow, own = prompt.library_setting_buttons(
        player, GUILD, uses, draft=draft, library=await library_for(GUILD))
    view = View()
    for b in (status, borrow, own):
        view.add_item(b)
    return status, borrow, own


async def test_the_row_shows_the_saved_answer_like_the_entry_cap(test_db, shelf):
    await _lounge()
    for uses, label in [(True, "Library: ON 🏛️"), (False, "Library: OFF 🃏"),
                        (None, "Library: not set")]:
        status, borrow, own = prompt.library_setting_buttons(PLAYER, GUILD, uses)
        assert status.label == label and status.disabled
        assert {b.row for b in (status, borrow, own)} == {prompt.LIBRARY_ROW}


async def test_borrow_saves_it_relabels_the_panel_and_holds_the_copy(test_db, shelf):
    await _lounge(stock={"Swamp": 4})
    await _drafted(PLAYER, 3)
    status, borrow, _ = await _row_buttons(False)
    interaction = _panel_interaction()

    await borrow.callback(interaction)

    assert await prefs.get_uses_library(PLAYER, GUILD) is True
    assert status.label == "Library: ON 🏛️", "relabelled where they pressed it"
    interaction.response.edit_message.assert_awaited_once()
    sent = interaction.followup.send.await_args
    assert "held for this draft" in sent.args[0], sent.args[0]
    assert prompt.AGREE_LABEL in _labels(sent.kwargs["view"])


async def test_bring_my_own_saves_it_and_lets_the_copy_go(test_db, shelf):
    await _lounge(stock={"Swamp": 4})
    await _drafted(PLAYER, 3)
    await prefs.set_uses_library(PLAYER, GUILD, True)
    status, _, own = await _row_buttons(True)
    from services.library_reservation import reserve, still_reserved
    assert (await reserve("q1")).status == "reserved"
    interaction = _panel_interaction()

    await own.callback(interaction)

    assert await prefs.get_uses_library(PLAYER, GUILD) is False
    assert status.label == "Library: OFF 🃏"
    assert "bring your own cards" in interaction.followup.send.await_args.args[0]
    assert not await still_reserved(await _row())


async def test_nobody_else_can_press_your_row(test_db, shelf):
    await _lounge()
    _, borrow, _ = await _row_buttons(None)
    interaction = _panel_interaction(MEMBER)

    await borrow.callback(interaction)

    assert await prefs.get_uses_library(PLAYER, GUILD) is None
    interaction.response.edit_message.assert_not_awaited()


# --- the Library button on a premade board -----------------------------------

async def test_a_premade_board_lists_only_servers_with_a_rental_library(test_db):
    import services.library_boards as boards
    await _lounge()
    boards._BOARDS.clear()

    await boards.note_board("lib-board", GUILD)
    await boards.note_board("plain-board", "some-other-guild")

    assert boards.has_library_button("lib-board")
    assert not boards.has_library_button("plain-board")


async def test_open_boards_are_loaded_before_they_are_re_attached(test_db):
    import services.library_boards as boards
    await _lounge()
    await _queue([PLAYER], "open-q")
    boards._BOARDS.clear()

    assert await boards.load_boards() == 1
    assert boards.has_library_button("open-q")


async def test_the_library_button_appears_only_on_those_boards(test_db):
    import services.library_boards as boards
    from views import PersistentView

    def labels(session_type, sid):
        view = PersistentView(bot=None, draft_session_id=sid, session_type=session_type)
        return [getattr(c, "label", None) for c in view.children]

    boards._BOARDS.clear()
    boards._BOARDS.add("lib-board")
    for kind in ("premade", "random", "swiss", "winston"):
        assert "Library 🏛️" in labels(kind, "lib-board"), kind
        assert "Library 🏛️" not in labels(kind, "plain-board"), kind
    assert "Library 🏛️" not in labels("staked", "lib-board"), \
        "a staked board has the row on Change Entry / Settings already"


async def test_the_library_button_opens_the_row(test_db, shelf):
    await _lounge(stock={"Swamp": 4})
    await _drafted(PLAYER, 3)
    await _queue([PLAYER])
    interaction = _interaction(PLAYER)

    await prompt.library_settings_panel(interaction, "q1")

    sent = interaction.followup.send.await_args
    assert "remembered for drafts here" in sent.args[0], sent.args[0]
    assert _labels(sent.kwargs["view"]) == [
        "Library: not set", "Borrow from the library 🏛️", "Bring my own cards 🃏"]


async def test_the_library_button_says_so_when_you_cannot_borrow(test_db, shelf):
    await _lounge()
    await _queue([PLAYER])
    interaction = _interaction(PLAYER)

    await prompt.library_settings_panel(interaction, "q1")

    sent = interaction.followup.send.await_args
    assert "view" not in sent.kwargs and "nothing to set" in sent.args[0]


# --- when something goes wrong ----------------------------------------------

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
    assert "Change Entry / Settings" in said, said


async def test_a_server_with_no_library_is_not_told_anything(test_db):
    interaction = _interaction(MEMBER)

    await prompt.library_after_sign_up(interaction, SimpleNamespace(session_id="x", cube=CUBE))

    interaction.followup.send.assert_not_awaited()


# --- asking again after "don't need it", and things going wrong --------------

async def test_switching_back_on_after_dont_need_it_gets_the_copy_back(test_db, shelf):
    """Agreed, then "don't need it", then Borrow on the panel: the player must
    not be told "reserved, collect your deck" while still marked as released."""
    from services.library_reservation import library_users, release_player
    await _lounge(stock={"Swamp": 4})
    await _drafted(PLAYER, 3)
    await prefs.set_uses_library(PLAYER, GUILD, True)
    await prefs.set_uses_library(MEMBER, GUILD, True)
    await _queue([MEMBER, PLAYER])
    from services.library_reservation import agree_player
    await agree_player("q1", MEMBER)
    await agree_player("q1", PLAYER)
    await release_player("q1", PLAYER)
    from services.library_service import library_for
    _, borrow, _ = prompt.library_setting_buttons(
        PLAYER, GUILD, True, draft=await _row(), library=await library_for(GUILD))
    from discord.ui import View
    View().add_item(borrow)
    interaction = _panel_interaction()

    await borrow.callback(interaction)

    assert PLAYER in await library_users(await _row()), "back among those who need it"
    said = interaction.followup.send.await_args.args[0]
    assert "reserved for this draft" in said, said


async def test_a_yes_that_cannot_reserve_says_it_was_saved_and_how_to_retry(
        test_db, shelf, monkeypatch):
    await _lounge(stock={"Swamp": 4})
    await _drafted(PLAYER, 3)
    view = await _view()
    monkeypatch.setattr(prompt, "agree_player",
                        AsyncMock(side_effect=RuntimeError("db gone")))
    button = next(b for b in view.children if b.uses_library is True)
    interaction = _interaction(PLAYER)

    await button.callback(interaction)

    interaction.response.defer.assert_awaited_once()
    assert await prefs.get_uses_library(PLAYER, GUILD) is True
    said = interaction.edit_original_response.await_args.kwargs["content"]
    assert "Saved" in said and "Couldn't check the library" in said, said


async def test_the_library_button_answers_even_when_the_row_cannot_be_built(
        test_db, shelf, monkeypatch):
    await _lounge()
    await _queue([PLAYER])
    monkeypatch.setattr(prompt, "library_to_ask_about",
                        AsyncMock(side_effect=RuntimeError("db gone")))
    interaction = _interaction(PLAYER)

    await prompt.library_settings_panel(interaction, "q1")

    interaction.response.defer.assert_awaited_once()
    assert interaction.followup.send.await_args.args[0] == prompt.COULD_NOT_CHECK


async def test_a_board_rebuilt_after_startup_keeps_its_library_button(test_db):
    """What re_register_views does on a restart, after on_ready has loaded the
    boards: the view it re-attaches must still carry the button."""
    import services.library_boards as boards
    from views import PersistentView
    await _lounge()
    await _queue([PLAYER], "open-q")
    boards._BOARDS.clear()

    await boards.load_boards()
    for stage in (None, "teams"):
        view = PersistentView(bot=None, draft_session_id="open-q",
                              session_type="premade", session_stage=stage)
        assert "Library 🏛️" in [getattr(c, "label", None) for c in view.children]


async def test_changing_entry_or_team_keeps_a_dont_need_it(test_db, shelf):
    """The sign-up path also runs on an entry change or a team switch; neither
    is asking again, so a player who said "don't need it" stays released and is
    not told the copy is theirs."""
    from services.library_reservation import (agree_player, library_users,
                                              release_player)
    await _lounge(stock={"Swamp": 4})
    await _drafted(PLAYER, 3)
    for p in (MEMBER, PLAYER):
        await prefs.set_uses_library(p, GUILD, True)
    await _queue([MEMBER, PLAYER])
    await agree_player("q1", MEMBER)
    await agree_player("q1", PLAYER)
    await release_player("q1", PLAYER)
    interaction = _interaction(PLAYER)

    await prompt.library_after_sign_up(interaction, SimpleNamespace(session_id="q1", cube=CUBE))

    assert PLAYER not in await library_users(await _row())
    said = interaction.followup.send.await_args.args[0]
    assert "held for this draft" in said and prompt.AGREE_LABEL in said, said


async def test_the_library_step_runs_in_the_background_and_is_kept_alive(test_db):
    """Started after the confirmation and not awaited by the sign-up, so it is
    held until it finishes rather than left to the loop's weak reference."""
    import asyncio
    from unittest.mock import patch
    ran = asyncio.Event()

    async def step(interaction, draft):
        await asyncio.sleep(0)
        ran.set()

    with patch.object(prompt, "library_after_sign_up", step):
        task = prompt.library_after_sign_up_soon(_interaction(), SimpleNamespace(session_id="q1"))
        assert task in prompt._RUNNING
        await asyncio.wait_for(task, 1)

    assert ran.is_set() and task not in prompt._RUNNING
