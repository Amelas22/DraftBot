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


def test_a_whole_drafted_pool_being_short_still_fits_in_a_discord_message():
    """Discord refuses a message over 2000 characters, and the send RAISES --
    so an over-long shortfall is not truncated, it gets no reply at all.

    Every loan used to be a hand-seeded fixture of two or three cards. A
    drafted pool is up to 48 distinct names, and a library that covers none of
    them names all 48. Reachable the obvious way: a second draft whose pools
    overlap a singleton library the first draft already emptied.
    """
    short = [{"name": f"Some Reasonably Long Card Name {i}", "want": 3, "have": 0}
             for i in range(48)]

    message = describe_shortfall(short)

    assert len(message) < 2000, f"{len(message)} characters"
    assert "more" in message, "and it says how many it could not list"


def test_a_short_list_is_not_truncated_and_says_nothing_about_more():
    short = [{"name": "Swamp", "want": 4, "have": 1},
             {"name": "Island", "want": 2, "have": 0}]

    message = describe_shortfall(short)

    assert "more" not in message
    assert "Swamp" in message and "Island" in message
