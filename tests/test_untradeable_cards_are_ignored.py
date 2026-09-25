"""A cube's paper-only cards must not be counted against the library.

Conspiracies and the Conspiracy sets' draft-matters cards have no MTGO
printing, so the library can never hold them however much anybody deposits.
Counting them as a gap makes a cube that drafts them permanently unsupported --
the board would say "bring your own cards" forever, for cards nobody could
supply. Leaving them out of the deposit matters more: the serve refuses a whole
order containing a name it does not know, so one of these costs every other
card in the trade.
"""
import pytest
import services.card_library_inventory as inventory

import cogs.library_commands as deposit_mod
import cube_views.pack_options as mod
from conftest import a_library
from cube_views.pack_options import library_signup_note

GUILD = "g-untradeable"
CUBE = "conspiracy-cube"

# A cube the library covers completely, except for cards MTGO has never had.
CUBE_CARDS = [
    {"name": "Swamp", "qty": 4},
    {"name": "Cogwork Librarian", "qty": 1},
    {"name": "Sovereign's Realm", "qty": 1},
]
SHELF = {"Swamp": 4}


def _shelf(monkeypatch, held, available, cards):
    async def _held(_library_id):
        return dict(held)

    async def _avail(_library_id, *_a, **_k):
        return dict(available)

    async def _fetch(_cube_id):
        return cards
    monkeypatch.setattr(mod, "library_holdings", _held)
    monkeypatch.setattr(mod, "library_available", _avail)
    monkeypatch.setattr(inventory, "fetch_cube", _fetch)


@pytest.mark.asyncio
async def test_a_cube_is_covered_though_its_conspiracies_never_can_be(
        test_db, monkeypatch):
    """The library holds every card it could ever hold, so the board says so."""
    await a_library("lib", guild=GUILD, collateral=0, cubes=(CUBE,))
    _shelf(monkeypatch, SHELF, SHELF, CUBE_CARDS)

    note = await library_signup_note(CUBE, GUILD)

    assert "no cards needed" in note.lower(), note


@pytest.mark.asyncio
async def test_what_a_deposit_offers_has_already_been_through_the_doorway(test_db):
    """The rule is no longer cards_to_deposit's to remember.

    A cube reaches the library only through cube_as_the_library_sees_it, which
    takes out what MTGO has never had, so the top-up arithmetic downstream can
    only ever see cards that can actually cross. This asserts the composition
    rather than re-testing the filter, which has its own tests.
    """
    await a_library("lib2", guild="g-dep", collateral=0, cubes=(CUBE,))

    async def _fetch(_cube_id):
        return CUBE_CARDS
    seen = await inventory.cube_as_the_library_sees_it(CUBE, fetch=_fetch)
    offering = await deposit_mod.cards_to_deposit(
        seen.cards, "lib2", full_copy=False, copies=1)

    assert [c["name"] for c in offering] == ["Swamp"]
    assert seen.not_on_mtgo == ["Cogwork Librarian", "Sovereign's Realm"]


def test_nothing_is_said_when_every_card_can_cross():
    """No note at all for the ordinary cube, which is almost every cube."""
    assert deposit_mod.left_out_note([]) == ""


def test_the_depositor_is_told_which_cards_were_left_out():
    """Named rather than counted: the cube's owner can only fix a list they
    can see, and this is the one place anybody learns these cards are a
    problem."""
    note = deposit_mod.left_out_note(["Cogwork Librarian", "Sovereign's Realm"])

    assert "Cogwork Librarian" in note
    assert "Sovereign's Realm" in note
    assert "2" in note
    assert "MTGO" in note


def test_one_card_reads_as_one_card():
    note = deposit_mod.left_out_note(["Cogwork Librarian"])

    # Pinned as a whole sentence: an `or` here would pass while half the
    # singular grammar was wrong, which is the only way this can break.
    assert "1 card isn't on MTGO, so it was left out" in note, note
    assert "Cogwork Librarian" in note
