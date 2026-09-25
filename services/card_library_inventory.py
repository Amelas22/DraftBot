"""What the card library owns, and what of it can be lent right now.

Two questions, two answers, and conflating them is the mistake this exists to
prevent. A donor asking "what does this cube still need?" means everything on
the shelf. A drafter asking "can I borrow this?" means what is not already
spoken for -- a cube being drafted at this moment has its cards in players'
hands and may not support a second draft alongside it.

Read from the LEDGER, not from the serve's `/vault`. The vault truncates its
listing, which is why the withdrawal path has to presume an unlisted card is
present; it cannot answer "how many of X do we hold". The ledger is the claim of
record and is complete. The serve stays the authority at collection time, where
`/borrow` trims against what is physically there.

Attribution survives underneath: these are projections of the same per-donor
positions, not a replacement for them, which is what keeps "this drafter donated
the cube" reachable later without another model.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from loguru import logger
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from helpers.cube_list import fetch_cube
from helpers.mtgo_untradeable import split_untradeable
from services.card_substitution_service import to_mtgo_names
from models.card_loan import ACTIVE_STATES, CardLoan
from models.draft_session import DraftSession
from models.library_server import LibraryServer
from services import debt_service, wallet_service

# Loan states whose cards are not on the shelf: every state a loan can be in
# without being finished.
#
# ACTIVE_STATES itself rather than a list of its own, and that is the point. A
# state was added to that tuple and not to this one, and the two disagreeing is
# the whole bug: `dispatch_unknown` means a trade MAY be open and moving these
# cards, and a copy of the list that had not heard of it left them reservable
# by somebody else while the first borrower may already be holding them.
#
# `assigned` counts too: an assignment is a reservation, and the drafter is
# about to collect it. The rest are physically gone until a return settles.
SPOKEN_FOR = ACTIVE_STATES

# How long a draft may hold its whole cube before we stop believing it.
#
# Measured over 562 drafts: teams forming to decks assigned is 20 minutes
# median, 28 at p99, and never exceeded 31. A draft still unassigned an hour
# later is not drafting -- it broke -- and holding its cube indefinitely would
# make that cube undraftable by anyone, which is how the 2474 sessions still
# stuck at 'pairings' in the history would have poisoned every cube.
DRAFTING_WINDOW = timedelta(minutes=60)

# Stages a draft is no longer consuming its cube in.
FINISHED_STAGES = ("completed", "abandoned")


@dataclass
class LibraryCubeList:
    """A cube as the library deals in it.

    `cards` speak MTGO's vocabulary, so they can be compared to custody and
    sent as an order without further thought. `not_on_mtgo` keeps the CUBE's
    own names, because it is shown to the cube's owner and those are the names
    their list uses.
    """
    cards: "list[dict[str, Any]]"
    not_on_mtgo: "list[str]"


async def cube_as_the_library_sees_it(
    cube_id: Any, fetch: "Optional[Callable[[str], Any]]" = None,
) -> "Optional[LibraryCubeList]":
    """Read a cube and put it in the terms the library works in.

    THE doorway. Every consumer of a cube list -- the deposit, the two coverage
    checks, and what a draft is holding -- goes through here, so the two rules
    that make a CubeCobra list usable are applied once instead of being a
    convention each caller has to remember:

      * cards MTGO has never had are taken out (it refuses an order naming one,
        and refuses it whole), and
      * everything else is renamed to what MTGO calls it.

    Applying these per consumer is what left `_being_drafted` without them: a
    Universes Beyond card in a drafting cube never matched the ledger's name for
    it, so it reserved nothing and stayed lendable to a second draft at once.

    None for a cube that could not be read, mirroring fetch_cube -- "the cube is
    empty" and "CubeCobra did not answer" lead to different messages.
    """
    cards = await (fetch or fetch_cube)(str(cube_id))
    if cards is None:
        return None
    kept, dropped = split_untradeable(cards)
    return LibraryCubeList(cards=await to_mtgo_names(kept), not_on_mtgo=dropped)


async def library_holdings(library_id: Any) -> "dict[str, int]":
    """Every card THIS library owns, summed across its donors: {name: copies}.

    What a library holds IS the sum of what it owes its donors back, so this is
    the ledger's own netting asked without a player rather than a second way of
    counting cards.

    Scoped per library, and this is the only thing that separates them: several
    libraries share one MTGO account, so the vault holds their cards mixed
    together and cannot be asked whose is whose. Ask it unscoped and Cube Night
    would be told it owns the Lounge's Power.
    """
    if not library_id:
        return {}
    return await debt_service.get_cards_owed_by(
        wallet_service.library_scope(library_id), wallet_service.HOUSE_LIBRARY)


async def library_available(
    library_id: Any,
    fetch: "Optional[Callable[[str], Any]]" = None,
    *, exclude_loan_id: Optional[int] = None,
) -> "dict[str, int]":
    """What THIS library could actually lend right now: {name: copies}.

    Holdings minus what is spoken for. The two sources of commitment are
    phase-disjoint by construction rather than by arithmetic, which is why they
    add rather than needing a union: a draft holds its WHOLE cube only while it
    has produced no loans, and loans exist only once decks are assigned. No card
    can be counted by both.

    `fetch` reads a cube's list and defaults to CubeCobra; tests pass their own
    so nothing here reaches the network.
    """
    held = await library_holdings(library_id)
    spoken_for = await _on_loan(library_id, exclude_loan_id=exclude_loan_id)
    for name, qty in (await _being_drafted(fetch or fetch_cube, library_id)).items():
        spoken_for[name] = spoken_for.get(name, 0) + qty

    available: "dict[str, int]" = {}
    for name, qty in held.items():
        left = qty - spoken_for.get(name, 0)
        # Never negative. More can be out than the ledger shows held -- a seeded
        # loan, a repair, a cube naming a card nobody donated -- and a negative
        # would subtract from the next card in any sum built on this.
        if left > 0:
            available[name] = left
    return available


async def _on_loan(library_id: Any, *, exclude_loan_id: Optional[int] = None
                   ) -> "dict[str, int]":
    """Cards committed by THIS library's loans: assigned, in flight, out, or
    coming back. Another library's loans draw down its own shelf, not this
    one's, however much the two share an MTGO account."""
    async with AsyncSessionLocal() as session:
        loans = list((await session.scalars(
            select(CardLoan).where(
                CardLoan.state.in_(SPOKEN_FOR),
                CardLoan.library_id == str(library_id)))).all())

    out: "dict[str, int]" = {}
    for loan in loans:
        if loan.id == exclude_loan_id and loan.state == "assigned":
            continue  # Collecting this reservation must not subtract it twice.
        cards = loan.offered_cards if loan.state == "out_pending" else loan.cards
        if loan.state in ("borrowed", "return_pending"):
            positions = await debt_service.get_open_card_positions(
                wallet_service.library_scope(library_id), str(loan.borrower_id),
                wallet_service.HOUSE_MTGO)
            owed = [{"name": p["card_name"], "qty": -p["net"]}
                    for p in positions if p["net"] < 0]
            # The original deck remains the draft record. A partial handover
            # commits only what crossed; a loan whose claim has not been booked
            # yet reserves its whole deck, which is the conservative direction.
            cards = owed or cards
        for card in (cards or []):
            name = card.get("name")
            if name:
                out[name] = out.get(name, 0) + int(card.get("qty") or 0)
    return out


async def _being_drafted(fetch: "Callable[[str], Any]",
                         library_id: Any) -> "dict[str, int]":
    """The full card lists of THIS library's drafts that are underway but not
    yet assigned.

    Between teams forming and decks being assigned the packs are dealt but the
    pools are not recorded anywhere, so there is no way to say which cards went
    to whom -- the whole cube is at risk rather than the part that happens to
    have been borrowed. Once any loan exists for the draft, the assignment is
    the precise answer and replaces this one.

    Restricted to the servers this library serves. A draft in a room drawing on
    a different library takes its cards from that library's shelf, and holding
    this one's cube against it would make a cube undraftable because an
    unrelated community happened to be playing it.
    """
    cutoff = datetime.now() - DRAFTING_WINDOW
    async with AsyncSessionLocal() as session:
        guilds = [str(g) for g in (await session.scalars(
            select(LibraryServer.guild_id).where(
                LibraryServer.library_id == str(library_id)))).all()]
        if not guilds:
            return {}
        underway = list((await session.scalars(
            select(DraftSession).where(
                DraftSession.teams_start_time.isnot(None),
                DraftSession.teams_start_time >= cutoff,
                DraftSession.cube.isnot(None),
                DraftSession.guild_id.in_(guilds),
            ))).all())
        if not underway:
            return {}
        assigned = {
            str(s) for s in (await session.scalars(
                select(CardLoan.source).where(
                    CardLoan.source.in_([f"draft:{d.session_id}" for d in underway])
                ))).all()
        }

    committed: "dict[str, int]" = {}
    for draft in underway:
        if draft.session_stage in FINISHED_STAGES:
            continue
        if f"draft:{draft.session_id}" in assigned:
            continue                      # its loans speak for it now
        # Through the doorway, so what a draft is holding is named the way
        # custody is. Comparing a raw CubeCobra list to the ledger meant a
        # Universes Beyond card reserved nothing -- its cube name never matched
        # the MTGO name it was booked under -- and those copies stayed lendable
        # while a draft had them on the table.
        seen = await cube_as_the_library_sees_it(draft.cube, fetch=fetch)
        cards = seen.cards if seen else None
        if not cards:
            # CubeCobra could not be read. Holding nothing is the safe way to be
            # wrong: the shelf stays lendable and a borrow that overreaches is
            # trimmed to what is there. Holding everything would refuse every
            # borrow in the guild while the library sat full.
            logger.warning("library: cube {} for draft {} could not be read; not "
                           "holding it against availability", draft.cube,
                           draft.session_id)
            continue
        for card in cards:
            name = card.get("name")
            if name:
                committed[name] = committed.get(name, 0) + int(card.get("qty") or 0)
    return committed


@dataclass
class Support:
    """Whether a cube can be drafted from a given shelf, and what it lacks."""

    ok: bool
    missing: "list[dict[str, Any]]" = field(default_factory=list)

    @property
    def cards_short(self) -> int:
        """Total copies needed, not distinct names -- the number a donor acts on."""
        return sum(int(m["short"]) for m in self.missing)


def cube_support(cube_cards: "list[dict[str, Any]]",
                 shelf: "dict[str, int]") -> Support:
    """Can this cube be drafted from `shelf`?

    Supported when every card clears its own quantity. Drafting is without
    replacement, so a cube running three Lightning Bolt needs three copies --
    counting distinct names would call a singleton library enough for a cube
    that triples half its list.

    Takes the shelf rather than fetching one, because the same comparison
    answers two different questions and both are wanted:

        cube_support(cube, await library_holdings(lib))   does the library OWN enough
        cube_support(cube, await library_available(lib)) can it be drafted TODAY

    The first is the donor's question and its `missing` is the shopping list for
    keeping a cube supported as it changes; the second is the drafter's.

    `missing` keeps the cube's own order rather than sorting, so the list reads
    the way the cube does.
    """
    missing: "list[dict[str, Any]]" = []
    for card in cube_cards:
        name = card.get("name")
        if not name:
            continue
        want = int(card.get("qty") or 0)
        have = int(shelf.get(name, 0))
        if have < want:
            missing.append({"name": name, "want": want, "have": have,
                            "short": want - have})
    return Support(ok=not missing, missing=missing)
