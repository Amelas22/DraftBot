"""How much a borrower puts up, and why it differs between two of them.

A rental library stocks somebody else's cards, so the hold is the only thing
standing between a stranger and a deck they could keep. One figure for everyone
has to be set for the worst borrower it might meet, which prices the people the
library already trusts as though nobody knew them.

So there are two figures, and what separates them is the library's MEMBER LIST:
the people somebody chose to trust with these cards pay the lower one, and
everybody else the library lends to -- anybody with enough drafts in its
servers, where it has a threshold (services/library_access_service) -- pays the
higher.

Not the return record, which sounds more apt and is not usable: measured on
2026-10-08, 23 decks had ever been collected and all 23 came back, so there was
no default history to price. The one signal that looks like misbehaviour is the
reminder, and 17 of those 23 loans had one -- because the first reminder fires
an hour after the draft and the median deck is back in two. Rating on returns
would mark the library's best borrowers untrustworthy.
"""
from typing import Any, Optional

from loguru import logger

from services.library_access_service import members
from services.library_service import price_range_of


async def is_trusted(library_id: Any, player_id: Any) -> bool:
    """Whether this library asks this borrower its lower figure."""
    return str(player_id) in await members(library_id)


async def hold_for(library: Any, player_id: Any) -> Optional[int]:
    """What THIS borrower puts up for THIS library, or None if it cannot lend.

    None rather than 0 for a missing library, the same distinction price_of
    makes: no library is a refusal, and a library that genuinely charges nothing
    is a choice somebody made.

    A library with no untrusted figure is not tiered and charges everyone its
    one price -- which is every library until somebody sets the second number,
    so adding this column changed nothing for anyone by itself.

    Asked per borrower, which the model's "one number per library" rule permits:
    that rule is about there being no per-CUBE figure for anyone to argue over.
    Both numbers here are still the library's own policy, set from the same shell
    tool, and a borrower cannot move themselves between them.
    """
    prices = price_range_of(library)
    if prices is None:
        return None
    least, most = prices
    if least == most or await is_trusted(library.id, player_id):
        return least
    logger.info("library {}: {} is not on its member list, so the "
                "hold is {} rather than {}", library.id, player_id, most, least)
    return most
