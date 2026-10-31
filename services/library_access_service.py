"""Who may borrow from a library that is not open to everyone.

Only BORROWING is gated. Depositing, /mydeposits and /withdraw stay open to
anyone: somebody contributing cards is not the risk this guards against, and a
sponsor locked out of their own deposits would be absurd.

Scoped to the LIBRARY, not the server. Somebody trusted with a sponsor's cards
is trusted with them in every room that library lends into, and a server-scoped
list meant re-inviting the same people per room -- and, worse, left the library
open in any room nobody had listed yet.

Two things can let somebody in: the member list, and a draft count. Without a
threshold (`Library.min_drafts_to_borrow` NULL) the list alone decides, as it
always has -- nobody named lends to everyone, anybody named restricts it to the
named. With one, a member still gets in, and so does anybody with that many
drafts in the library's servers; the list then mostly decides who pays the
lower hold (services/library_trust_service).
"""
from typing import Any, Iterable, Optional

from loguru import logger
from sqlalchemy import delete, func, select

from database.db_session import AsyncSessionLocal
from models.library import Library
from models.library_member import LibraryMember
from models.library_server import LibraryServer
from models.player import PlayerStats
from services.library_tournament_exemption import exempt_among


async def members(library_id: Any) -> "list[str]":
    """Everyone named on this library's list. Empty means nobody is named."""
    if not library_id:
        return []
    async with AsyncSessionLocal() as session:
        return [str(p) for p in (await session.scalars(
            select(LibraryMember.player_id).where(
                LibraryMember.library_id == str(library_id)))).all()]


async def min_drafts(library_id: Any) -> Optional[int]:
    """The draft count that lets a non-member borrow, or None if there is none."""
    if not library_id:
        return None
    async with AsyncSessionLocal() as session:
        library = await session.get(Library, str(library_id))
    if library is None or library.min_drafts_to_borrow is None:
        return None
    return int(library.min_drafts_to_borrow)


async def drafts_played_many(library_id: Any,
                             player_ids: "Iterable[Any]") -> "dict[str, int]":
    """How many drafts each player has taken part in where this library lends.

    Summed across the library's servers rather than asked of one, because the
    library is the unit of policy everywhere else in this feature: somebody
    established in any room it lends into is established as far as it is
    concerned.

    PlayerStats is a maintained counter keyed (player_id, guild_id), so this is
    one indexed read rather than a scan of draft history -- which matters
    because it is asked on the borrow path, while a player is waiting. It moves
    when a draft's rooms and pairings are made (update_player_stats_for_draft),
    so at sign-up the draft being joined does not count yet, and by the time its
    decks are offered it does.

    A player with no row has played nothing here and counts 0.
    """
    ids = [str(p) for p in player_ids]
    counts = dict.fromkeys(ids, 0)
    if not library_id or not ids:
        return counts
    async with AsyncSessionLocal() as session:
        guilds = select(LibraryServer.guild_id).where(
            LibraryServer.library_id == str(library_id))
        rows = (await session.execute(
            select(PlayerStats.player_id, func.sum(PlayerStats.drafts_participated))
            .where(PlayerStats.player_id.in_(ids),
                   PlayerStats.guild_id.in_(guilds))
            .group_by(PlayerStats.player_id))).all()
    for player_id, total in rows:
        counts[str(player_id)] = int(total or 0)
    return counts


async def is_invite_only(library_id: Any) -> bool:
    """Does this library lend only to named people?

    Asked by the signup board, which is shared and cannot know who is reading
    it -- so it says the library is restricted rather than promising a deck to
    a room where most people would be turned away at /borrow. A library with a
    draft threshold is not invite-only: it lends to everybody who has drafted
    enough, and the board says so.
    """
    return bool(await members(library_id)) and await min_drafts(library_id) is None


async def may_borrow_many(library_id: Any,
                          player_ids: "Iterable[Any]") -> "set[str]":
    """The players among these who may take a deck out of this library.

    One read of the list and, where there is a threshold, one of the draft
    counts -- so deck assignment can ask for a whole pod at once and still use
    exactly the rule /library borrow does.
    """
    ids = [str(p) for p in player_ids]
    if not library_id:
        return set()
    listed = set(await members(library_id))
    threshold = await min_drafts(library_id)
    if threshold is None:
        allowed = set(ids) if not listed else {p for p in ids if p in listed}
    else:
        counts = await drafts_played_many(library_id, [p for p in ids if p not in listed])
        allowed = {p for p in ids if p in listed or counts.get(p, 0) >= threshold}
    # A running tournament can let its players past both (library_tournament_exemption).
    return allowed | await exempt_among(library_id, [p for p in ids if p not in allowed])


async def may_borrow(library_id: Any, player_id: Any) -> bool:
    """Is this person allowed to take a deck out of this library?

    A library with nobody listed and no threshold lends to everyone in the
    servers it serves -- the communal case, which needs no setup and must keep
    working untouched. Naming anybody restricts borrowing to the named, unless
    the library also lends to anybody with enough drafts there.
    """
    return str(player_id) in await may_borrow_many(library_id, [player_id])


async def why_not(library_id: Any, player_id: Any) -> str:
    """What to tell somebody may_borrow just refused."""
    threshold = await min_drafts(library_id)
    if threshold is None:
        return ("🔒 This card library is invite-only at the moment, and you're "
                "not on the list. Ask whoever runs it if you'd like to be.")
    played = (await drafts_played_many(library_id, [player_id]))[str(player_id)]
    return (f"🔒 This card library lends to players with {threshold}+ drafts "
            f"here, and you have {played}.")


async def invite(library_id: Any, player_id: Any, added_by: str) -> bool:
    """Trust somebody to borrow. True if this was the first member of a library
    with no draft threshold, which CLOSES it to everyone else -- the caller is
    expected to say so."""
    async with AsyncSessionLocal() as session:
        already = await members(library_id)
        if str(player_id) in already:
            return False
        session.add(LibraryMember(library_id=str(library_id),
                                  player_id=str(player_id), added_by=added_by))
        await session.commit()
    return not already and await min_drafts(library_id) is None


async def uninvite(library_id: Any, player_id: Any) -> str:
    """Stop somebody borrowing. "removed", "not_listed", or "would_open".

    Refuses to remove the LAST member. Empty means open, so that removal turned
    a curated library into a public one and handed the person being removed the
    access that was being taken away -- the most expensive possible outcome of
    an operation whose whole purpose is to withdraw trust. Nothing about
    "remove Bob" says "and let everyone in", so it does not do that;
    open_library says it outright.

    "not_listed" because this used to report success for anybody at all: a
    mistyped id read as a revocation that had not happened, while the real
    member carried on borrowing.
    """
    listed = await members(library_id)
    if str(player_id) not in listed:
        return "not_listed"
    # Only where the list is the gate. With a threshold, an empty list leaves
    # the library lending to the experienced, which is no wider than before.
    if len(listed) == 1 and await min_drafts(library_id) is None:
        return "would_open"
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(LibraryMember).where(
                LibraryMember.library_id == str(library_id),
                LibraryMember.player_id == str(player_id)))
        await session.commit()
    return "removed"


async def open_library(library_id: Any) -> int:
    """Let everyone borrow from this library again. Returns how many were listed.

    The deliberate way to reach the state uninvite refuses to fall into.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(LibraryMember).where(
                LibraryMember.library_id == str(library_id)))
        await session.commit()
    removed = int(result.rowcount or 0)
    logger.info("library: {} is open to everyone again ({} member(s) cleared)",
                library_id, removed)
    return removed
