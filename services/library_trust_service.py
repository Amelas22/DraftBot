"""How much a borrower puts up, and why it differs between two of them.

A rental library stocks somebody else's cards, so the hold is the only thing
standing between a stranger and a deck they could keep. One figure for everyone
has to be set for the worst borrower it might meet, which prices the people who
have been drafting here for years as though nobody knew them.

So there are two figures, and what separates them is TENURE: how many drafts
this player has taken part in, in the servers this library lends into. Not their
return record, which sounds more apt and is not usable -- see below -- and not a
list somebody maintains by hand, which goes stale the moment attention moves.

Measured on 2026-10-08 in the Lounge, the one rental library in production: of
the nine players invited to borrow, eight have between 21 and 386 drafts and the
ninth has never played a rated draft here. Every threshold between 1 and 20
therefore sorts the current nine identically, which is why TRUSTED_DRAFTS is not
a finely tuned number -- it is a line drawn through an empty gap.

Why not the return record, which is the obvious answer: 23 decks have ever been
collected and all 23 came back, so there is no default history to price. The one
signal that looks like misbehaviour is the reminder, and 17 of those 23 loans
have one -- because the first reminder fires an hour after the draft and the
median deck is back in two. Rating on returns today would mark the library's
best borrowers untrustworthy and leave the rest unrated.
"""
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from models.library_server import LibraryServer
from models.player import PlayerStats

# Drafts in this library's servers that make a borrower a known quantity.
#
# Ten is "a regular, not a drifter": the Lounge's median player has 2 drafts and
# its upper quartile has 16, so ten sits in the gap between somebody passing
# through and somebody who turns up. It is deliberately well below the 21 of the
# least-drafted invited borrower, so that nobody who can borrow today is one
# cancelled draft away from their hold doubling.
TRUSTED_DRAFTS = 10


async def drafts_played(library_id: Any, player_id: Any) -> int:
    """How many drafts this player has taken part in where this library lends.

    Summed across the library's servers rather than asked of one, because the
    library is the unit of policy everywhere else in this feature: somebody
    established in any room it lends into is established as far as it is
    concerned.

    PlayerStats is a maintained counter keyed (player_id, guild_id), so this is
    one indexed read per server rather than a scan of draft history -- which
    matters because it is asked on the borrow path, while a player is waiting.

    A player with no row has played nothing here and counts as nobody, which is
    the answer that makes a stranger a stranger.
    """
    async with AsyncSessionLocal() as session:
        guilds = [str(g) for g in (await session.scalars(
            select(LibraryServer.guild_id).where(
                LibraryServer.library_id == str(library_id)))).all()]
        if not guilds:
            return 0
        counts = (await session.scalars(
            select(PlayerStats.drafts_participated).where(
                PlayerStats.player_id == str(player_id),
                PlayerStats.guild_id.in_(guilds)))).all()
    return sum(int(n or 0) for n in counts)


async def is_trusted(library_id: Any, player_id: Any) -> bool:
    """Whether this library treats this borrower as a known quantity."""
    return await drafts_played(library_id, player_id) >= TRUSTED_DRAFTS


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
    tool, and a borrower cannot move themselves between them except by drafting.
    """
    if library is None:
        return None
    trusted_price = int(library.collateral_tix or 0)
    untrusted_price = getattr(library, "collateral_tix_untrusted", None)
    if untrusted_price is None:
        return trusted_price
    if await is_trusted(library.id, player_id):
        return trusted_price
    logger.info("library {}: {} is not an established drafter here, so the "
                "hold is {} rather than {}", library.id, player_id,
                int(untrusted_price), trusted_price)
    return int(untrusted_price)
