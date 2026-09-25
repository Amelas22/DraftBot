"""Telling people which side of an MTGO trade to open.

MTGO has two: your whole collection, and a trade binder you curate. The bot
asks for cards by name, so the full trade list finds them wherever they are;
out of a binder it can only see what somebody remembered to put there, and a
short binder reads like the bot losing cards rather than like a list being
incomplete.

Returning is the case where it is not merely easier but safer: the bot takes
back the exact printing it lent, so a binder holding a different printing of
the same card leaves the trade unable to complete.
"""
import pytest

from helpers.money_gate import full_trade_list_advice


def test_it_points_at_the_full_trade_list_rather_than_the_binder():
    """The copy used to tell depositors to fill their binder first, which is
    the harder path and the one that goes wrong."""
    advice = full_trade_list_advice()

    assert "Full Trade List" in advice
    assert advice.index("Full Trade List") < advice.index("binder"), \
        "the easy path should be named first"


def test_a_return_warns_about_the_printing():
    """The bot takes back what it lent, not another copy of the same card."""
    advice = full_trade_list_advice(exact_printing=True)

    assert "printing" in advice
    assert "binder" in advice, "a binder still works, with care"


def test_giving_cards_does_not_mention_printings_when_it_need_not():
    """A deposit is cards the player owns; any printing is theirs to give, and
    a warning that does not apply is noise that makes the one that does apply
    easier to ignore."""
    assert "printing" not in full_trade_list_advice()


@pytest.mark.parametrize("exact", [False, True])
def test_the_advice_is_one_short_line(exact):
    """It sits under the trade prompt on a message that already carries a deck
    list and a job footer."""
    assert len(full_trade_list_advice(exact)) < 220


@pytest.mark.asyncio
async def test_only_the_side_that_HANDS_CARDS_OVER_gets_the_advice(monkeypatch):
    """Borrowing GIVES cards to the player; they offer nothing, so telling them
    how to open a trade list is instructions for something they are not doing.
    Returning is the direction where it matters, and matters most -- the bot
    takes back the exact printing it lent.

    Driven through both commands rather than read off the source: asserting on
    a fragment of an f-string pinned its spacing and quote style, so a
    reformat broke it with no change in behaviour at all, while a real
    condition flip would have gone unnoticed.
    """
    from test_library_commands import _run

    collecting = await _run(monkeypatch, "borrow", "dispatched")
    giving_back = await _run(monkeypatch, "return_cards", "dispatched")

    assert "Full Trade List" not in collecting
    assert "Full Trade List" in giving_back
    assert "printing" in giving_back.lower()
