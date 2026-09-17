"""Telling a borrower the library is short, and letting them take what there is.

"Couldn't borrow" is a dead end. The useful version names the card, the number
available and the number they must find elsewhere -- then offers them the
partial deck, because a player who can source two Swamps themselves would
rather have the other eight than nothing.
"""
from cogs.card_lending_commands import describe_shortfall

SHORT = [{"name": "Swamp", "want": 10, "have": 2},
         {"name": "Ghostly Wings", "want": 1, "have": 0}]


def test_it_names_the_card_what_is_there_and_what_is_missing():
    text = describe_shortfall(SHORT)
    assert "Swamp" in text
    assert "2" in text and "10" in text, "what there is and what was wanted"
    assert "8" in text, "and the number they have to source themselves"


def test_a_card_with_none_at_all_says_so_rather_than_offering_zero():
    text = describe_shortfall([{"name": "Ghostly Wings", "want": 1, "have": 0}])
    assert "Ghostly Wings" in text
    assert "none" in text.lower() or "0" in text


def test_nothing_short_produces_nothing():
    assert describe_shortfall([]) == ""
