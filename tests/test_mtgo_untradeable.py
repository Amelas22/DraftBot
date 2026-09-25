"""Cards MTGO has never had must not reach the serve.

The serve matches names exactly and refuses a whole order containing one it
does not know, so a single paper-only card in a 300-card deposit costs the
other 299 -- the same shape as the loan 19 incident that mtgo_names.py records.

Conspiracies and the Conspiracy sets' draft-matters cards are the known case:
those sets never came to MTGO, so no printing of them exists there.
"""
import pytest

from helpers.mtgo_untradeable import UNTRADEABLE, split_untradeable


def test_a_conspiracy_is_untradeable():
    assert "Sovereign's Realm" in UNTRADEABLE
    assert "Advantageous Proclamation" in UNTRADEABLE


def test_the_draft_matters_cards_are_untradeable():
    assert "Cogwork Librarian" in UNTRADEABLE
    assert "Lore Seeker" in UNTRADEABLE
    assert "Agent of Acquisitions" in UNTRADEABLE


@pytest.mark.parametrize("name", [
    # Every one of these is on MTGO, and every one is reported as paper-only by
    # the obvious check -- reading `games` off whatever single printing Scryfall
    # hands back, which is frequently a recent paper-only product. The list is
    # the guard against anyone "fixing" this with a per-printing lookup.
    "Goblin Welder", "Mind Stone", "Mother of Runes", "Wrath of God",
    "Fireball", "Exhume", "Flame Slash", "Oath of Druids",
])
def test_cards_whose_latest_printing_is_paper_only_are_still_tradeable(name):
    assert name not in UNTRADEABLE


def test_split_keeps_order_and_reports_what_it_dropped():
    cube = [
        {"name": "Lightning Bolt", "qty": 1},
        {"name": "Cogwork Librarian", "qty": 1},
        {"name": "Brainstorm", "qty": 2},
        {"name": "Sovereign's Realm", "qty": 1},
    ]

    kept, dropped = split_untradeable(cube)

    assert kept == [{"name": "Lightning Bolt", "qty": 1},
                    {"name": "Brainstorm", "qty": 2}]
    assert dropped == ["Cogwork Librarian", "Sovereign's Realm"]


def test_a_clean_cube_is_returned_unchanged():
    cube = [{"name": "Lightning Bolt", "qty": 1}]

    kept, dropped = split_untradeable(cube)

    assert kept == cube
    assert dropped == []


def test_dropped_names_are_not_repeated():
    """A cube listing one twice reports it once -- the message names cards to
    fix, not occurrences to count."""
    cube = [{"name": "Cogwork Librarian", "qty": 1},
            {"name": "Cogwork Librarian", "qty": 1}]

    kept, dropped = split_untradeable(cube)

    assert kept == []
    assert dropped == ["Cogwork Librarian"]
