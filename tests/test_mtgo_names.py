"""What MTGO calls a card with two faces.

Draftmancer names every two-faced card "Front // Back". MTGO does not: it keeps
the pair only for split and Aftermath cards, and names everything else -- the
transforming double-faced cards, the modal ones, the Adventures -- by the front
face alone. The serve matches on exact names, so getting this wrong does not
degrade gracefully; it refuses the whole trade.

Observed live on 2026-09-17: a drafted pool went out as
"Invasion of Ixalan // Belligerent Regisaur" and the borrow batch came back
`unknown card ... (names must be exact)`, taking the other forty-odd cards with
it.

The rows below use real card names in the SHAPE a stored log actually has,
which is the part worth being careful about: measured over 300 logs, a split
card carries `layout: "split"` and nothing else carries a `layout` key at all,
while `back` is present for transforming cards, ABSENT for Adventures and
splits alike, and never null. So `back` cannot be the discriminator -- the two
groups that need opposite answers both omit it.
"""
import pytest

from helpers.mtgo_names import mtgo_name


@pytest.mark.parametrize("card,expected", [
    # Split and Aftermath: MTGO keeps both halves, and these are the ONLY ones.
    ({"name": "Commit // Memory", "layout": "split-left"}, "Commit // Memory"),
    ({"name": "Fire // Ice", "layout": "split"}, "Fire // Ice"),
    ({"name": "Ticket Booth // Tunnel of Hate", "layout": "split"},
     "Ticket Booth // Tunnel of Hate"),
    ({"name": "Bedeck // Bedazzle", "layout": "split"}, "Bedeck // Bedazzle"),

    # Transforming: no layout key, a back face present. Front only.
    ({"name": "Invasion of Ixalan // Belligerent Regisaur",
      "back": {"name": "Belligerent Regisaur"}}, "Invasion of Ixalan"),
    ({"name": "Thing in the Ice // Awoken Horror",
      "back": {"name": "Awoken Horror"}}, "Thing in the Ice"),

    # Adventures: no layout key and NO back key either -- the same shape as the
    # split card two rows up, and the reason "does it have a back?" cannot tell
    # them apart while `layout` can.
    ({"name": "Bonecrusher Giant // Stomp"}, "Bonecrusher Giant"),
    ({"name": "Fae of Wishes // Granted"}, "Fae of Wishes"),

    # Ordinary cards are left exactly alone.
    ({"name": "Lightning Bolt"}, "Lightning Bolt"),
    ({"name": "Kongming, \"Sleeping Dragon\""}, "Kongming, \"Sleeping Dragon\""),
])
def test_mtgo_name(card, expected):
    assert mtgo_name(card) == expected


def test_a_card_with_no_name_stays_empty():
    assert mtgo_name({}) == ""


def test_a_slash_that_is_not_a_face_separator_is_left_alone():
    """MTGO's separator is " // " with spaces. A name that merely contains
    slashes is not a two-faced card and must not be truncated."""
    assert mtgo_name({"name": "Ratonhnhake:ton"}) == "Ratonhnhake:ton"
    assert mtgo_name({"name": "Borrowing 100,000 Arrows"}) == "Borrowing 100,000 Arrows"
