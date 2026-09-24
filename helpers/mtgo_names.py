"""What MTGO calls a card, given what Draftmancer calls it.

Draftmancer names every two-faced card `"Front // Back"`. MTGO keeps that pair
for split and Aftermath cards only, and names every other kind of two-faced card
-- transforming, modal, Adventure -- by its front face alone.

The serve matches names exactly and refuses an order containing one it does not
know, so the whole trade fails rather than the one card. That is what happened
to loan 19 on 2026-09-17: a drafted pool went out naming
`"Invasion of Ixalan // Belligerent Regisaur"` and forty-odd innocent cards went
down with it.

The discriminator is `layout`, and the obvious alternative does not work. "Does
it have a back face" fails because `back` is simply ABSENT for both Adventures
(`Bonecrusher Giant // Stomp`) and splits (`Fire // Ice`) -- the two groups that
need opposite answers. Measured over 300 stored draft logs: 2538 two-faced
entries carry a `back` dict, 1352 omit the key, and `back: null` never occurs at
all.

`layout` separates them cleanly in the same sample: 160 entries carry
`layout: "split"` and every one is a real split card; the other 3730 omit the
key entirely and are transforming, modal or Adventure cards, all of which MTGO
names by the front face. Aftermath cards are tagged `split-left`, which is why
the test is a prefix rather than equality.

Stripping every `//` instead would have broken every split card to fix the
double-faced ones -- and splits really do trade under their full name, as the
library's own movement record shows for `Turn // Burn`.
"""
from typing import Any

FACE_SEPARATOR = " // "


def mtgo_name(card: "dict[str, Any]") -> str:
    """The name to put in front of the serve for one Draftmancer card entry."""
    # str() rather than trusting the field: this runs over whatever Draftmancer
    # put in the log, and a non-string name would raise out of `in` and take the
    # entire draft's assignment down with it -- forty-five cards for one bad
    # entry, reported as "argument of type 'int' is not iterable".
    name = str(card.get("name") or "")
    if FACE_SEPARATOR not in name:
        return name
    if str(card.get("layout") or "").startswith("split"):
        # Fire // Ice is ONE MTGO card, and that is its name.
        return name
    return name.split(FACE_SEPARATOR, 1)[0]
