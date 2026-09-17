"""Splitting a cube across several trades, on THIS side of the wire.

Not the batching that was taken out. That was one order the serve split into
several jobs, with nothing tying the jobs back to the order -- so a scan that
saw some of them concluded the rest had failed. These are separate ORDERS, one
trade and one job each, settled by the same machinery that settles a single
deposit. The difference is where the split happens and therefore whether
anything has to be reassembled afterwards: nothing does.

A card whose own quantity exceeds a trade is split across chunks too, because
a 400-card stack of one name cannot go any other way.
"""
import pytest

from services.card_deposit_service import chunk_cards

LIMIT = 10


def _total(chunks):
    return sum(c["qty"] for chunk in chunks for c in chunk)


def test_a_list_that_fits_is_one_chunk():
    cards = [{"name": "Swamp", "qty": 4}, {"name": "Island", "qty": 6}]
    assert chunk_cards(cards, LIMIT) == [cards]


def test_a_longer_list_is_split_without_losing_a_card():
    cards = [{"name": f"Card {i}", "qty": 1} for i in range(25)]

    chunks = chunk_cards(cards, LIMIT)

    assert [sum(c["qty"] for c in chunk) for chunk in chunks] == [10, 10, 5]
    assert _total(chunks) == 25, "every card lands in exactly one chunk"


def test_a_single_card_bigger_than_a_trade_is_split_across_chunks():
    """A stack of one name cannot go any other way, and refusing it would make
    a cube undepositable for a reason its owner cannot act on."""
    chunks = chunk_cards([{"name": "Swamp", "qty": 25}], LIMIT)

    assert [sum(c["qty"] for c in chunk) for chunk in chunks] == [10, 10, 5]
    assert all(c["name"] == "Swamp" for chunk in chunks for c in chunk)
    assert _total(chunks) == 25


def test_a_chunk_never_exceeds_the_limit():
    cards = [{"name": "Swamp", "qty": 7}, {"name": "Island", "qty": 7},
             {"name": "Forest", "qty": 7}]

    chunks = chunk_cards(cards, LIMIT)

    assert all(sum(c["qty"] for c in chunk) <= LIMIT for chunk in chunks)
    assert _total(chunks) == 21


def test_quantities_are_merged_not_fragmented():
    """A card split across a boundary should still be one line per chunk, so
    the trade window reads as cards rather than as arithmetic."""
    chunks = chunk_cards([{"name": "Swamp", "qty": 12}], LIMIT)

    assert chunks == [[{"name": "Swamp", "qty": 10}], [{"name": "Swamp", "qty": 2}]]


def test_nothing_in_nothing_out():
    assert chunk_cards([], LIMIT) == []
