"""Telling people, on the signup board, whether they need their own cards.

A player deciding whether to join has one question the board never answered:
can I play this without owning the cards? Everything else about the library
lives where the DRAFT IS CREATED, which only the organiser sees.

Absent for cubes the library does not lend for, which is most of them -- a
field saying "you need your own cards" on every ordinary draft would be noise
on the majority to inform a minority.
"""
import pytest
import services.card_library_inventory as inventory

from database.db_session import AsyncSessionLocal
from conftest import a_library
import cube_views.pack_options as mod
from cube_views.pack_options import library_signup_note

GUILD, CUBE = "g1", "mycube"


async def _price(collateral=0):
    """Bind this server to a library that offers the cube, on these terms.

    The price belongs to the LIBRARY now: a server draws on one library and is
    bound to its terms, so there is no per-cube figure to set.
    """
    await a_library("lib", guild=GUILD, collateral=collateral,
                    cubes=(CUBE,))


def _shelf(monkeypatch, held, available, cards):
    # Both inventory reads take a library now and both ignore it here: which
    # library is the subject of its own tests, and these are about what the
    # board says once the shelf is known.
    async def _held(_library_id):
        return dict(held)

    async def _avail(_library_id, *_a, **_k):
        return dict(available)

    async def _fetch(cube_id):
        return cards
    monkeypatch.setattr(mod, "library_holdings", _held)
    monkeypatch.setattr(mod, "library_available", _avail)
    monkeypatch.setattr(inventory, "fetch_cube", _fetch)


CUBE_CARDS = [{"name": "Swamp", "qty": 4}]


@pytest.mark.asyncio
async def test_a_free_covered_cube_says_no_cards_needed(test_db, monkeypatch):
    await _price(0)
    _shelf(monkeypatch, {"Swamp": 4}, {"Swamp": 4}, CUBE_CARDS)

    note = await library_signup_note(CUBE, GUILD)

    assert "no cards needed" in note.lower(), note
    assert "free" in note.lower()


@pytest.mark.asyncio
async def test_a_priced_cube_names_what_borrowing_costs(test_db, monkeypatch):
    """Whether they can afford it is part of whether they can play."""
    await _price(25)
    _shelf(monkeypatch, {"Swamp": 4}, {"Swamp": 4}, CUBE_CARDS)

    note = await library_signup_note(CUBE, GUILD)

    assert "no cards needed" in note.lower()
    assert "25" in note


@pytest.mark.asyncio
async def test_a_cube_the_library_cannot_cover_says_bring_your_own(
        test_db, monkeypatch):
    """The case this exists for. Signing up expecting to borrow, and finding
    out at fire time, wastes the whole pod's evening."""
    await _price(0)
    _shelf(monkeypatch, {"Swamp": 4}, {"Swamp": 1}, CUBE_CARDS)

    note = await library_signup_note(CUBE, GUILD)

    assert "own cards" in note.lower(), note
    assert "no cards needed" not in note.lower()


@pytest.mark.asyncio
async def test_a_cube_the_library_does_not_lend_for_says_nothing(
        test_db, monkeypatch):
    """Most drafts have nothing to do with the library. A field on every one of
    them would be noise on the majority to inform a minority."""
    _shelf(monkeypatch, {"Swamp": 4}, {"Swamp": 4}, CUBE_CARDS)

    assert await library_signup_note(CUBE, GUILD) is None


@pytest.mark.asyncio
async def test_an_unreadable_cube_does_not_promise_anything(test_db, monkeypatch):
    """If we cannot check coverage we must not claim it. Saying "no cards
    needed" on a guess is the failure this is meant to prevent."""
    await _price(0)
    _shelf(monkeypatch, {"Swamp": 4}, {"Swamp": 4}, None)

    note = await library_signup_note(CUBE, GUILD)

    assert note is None or "no cards needed" not in note.lower()


@pytest.mark.asyncio
async def test_a_failure_to_check_does_not_break_the_signup_board(
        test_db, monkeypatch):
    """A draft being created must not fail because the library was unreadable."""
    await _price(0)

    async def boom():
        raise RuntimeError("ledger down")
    monkeypatch.setattr(mod, "library_available", boom)

    async def _fetch(cube_id):
        return CUBE_CARDS
    monkeypatch.setattr(inventory, "fetch_cube", _fetch)

    assert await library_signup_note(CUBE, GUILD) is None


# --- the field actually reaching the board ----------------------------------

def _embed_with_note(note):
    """Build a real signup embed the way a draft does, with the note set."""
    from unittest.mock import patch
    from test_draft_creation import (  # noqa: F401
        create_mock_interaction, create_session_details,
    )
    from sessions.random_session import RandomSession

    session = RandomSession(create_session_details(create_mock_interaction()))
    session.library_note = note
    with patch("sessions.base_session.get_cube_thumbnail_url",
               return_value="https://example.com/t.jpg"):
        return session.create_embed()


def test_the_note_reaches_the_signup_board():
    """A note nothing renders is a note nobody reads. This pins the wiring
    rather than the wording -- the function being right is not the same as the
    embed carrying it."""
    from cube_views.pack_options import LIBRARY_FIELD_NAME

    embed = _embed_with_note("🆓 **No cards needed** — borrow free.")

    field = next((f for f in embed.fields if f.name == LIBRARY_FIELD_NAME), None)
    assert field is not None, "the signup board must carry the library note"
    assert "No cards needed" in field.value


def test_a_draft_the_library_is_not_involved_in_gets_no_field():
    """Most drafts. The board must look exactly as it did before."""
    from cube_views.pack_options import LIBRARY_FIELD_NAME

    embed = _embed_with_note(None)

    assert not [f for f in embed.fields if f.name == LIBRARY_FIELD_NAME]


@pytest.mark.asyncio
async def test_a_paid_library_says_the_deposit_comes_back(
        test_db, monkeypatch):
    """Somebody deciding whether they can afford to play needs to know the
    deposit is returned -- 100 tix they get back is a very different
    proposition from 100 tix spent, and they need it BEFORE they sign up."""
    await _price(collateral=100)
    _shelf(monkeypatch, {"Swamp": 4}, {"Swamp": 4}, CUBE_CARDS)

    note = await library_signup_note(CUBE, GUILD)

    assert "100" in note
    assert "refund" in note.lower(), "the deposit must read as coming back"