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
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from loguru import logger
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from helpers.cube_list import fetch_cube
from helpers.mtgo_untradeable import split_untradeable
from helpers.stale_drafts import rooms_reaped
from services.card_substitution_service import to_mtgo_names
from models.card_loan import ACTIVE_STATES, CardLoan
from models.draft_session import DraftSession
from models.library_server import LibraryServer
from services import debt_service, wallet_service
from services.library_access_service import members
from services.library_request_service import active_requesters
from services.library_reminders import session_of

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

    Holdings minus what is spoken for. The three sources of commitment are
    phase-disjoint by construction rather than by arithmetic, which is why they
    add rather than needing a union. A draft holds its whole cube from sign-up
    (requested, no teams_start_time), then still holds it while dealing packs
    (underway, teams_start_time set, no loans yet), and from then on its loans
    speak for it. Each draft is in exactly one of those phases, so no card can
    be counted twice.

    `exclude_loan_id` asks on behalf of that loan, which does two things: the
    loan is not counted against itself, and the earlier reservation wins -- a
    draft that formed its teams after this loan's draft (see _reserved_since)
    holds nothing against it, whether still dealing packs or holding decks
    nobody has collected. A shelf with one copy of each card cannot serve two
    drafts of the same cube, and the one that reserved second waits. Cards
    somebody has actually collected count whichever draft they came from.

    A LOAN OUTRANKS EVERY SIGN-UP HOLD, which is why the third term is skipped
    entirely when asked on behalf of one. A draft handing out decks is never
    made to wait on a queue that has not fired: its players have already
    drafted, so refusing them is the harm this whole feature exists to prevent,
    while a queue can be told no and ask again. It is the rule for any such
    pair rather than for the earlier of the two, which is why nothing has to
    record when a request was made.

    The cost, stated because it is the one thing a hold does not cover: a draft
    that fires without anybody requesting still holds its whole cube (the term
    above, which asks nobody's permission), and its borrows will take cards a
    filling queue was holding. The queue's players find out by asking again,
    which is what the refusal tells them to do, and they find out while the
    queue is still filling rather than after drafting.

    A draft's own sign-up hold never blocks its own borrow: forming teams sets
    teams_start_time, so it leaves the third term and enters the second before
    any loan of its exists.

    `fetch` reads a cube's list and defaults to CubeCobra; tests pass their own
    so nothing here reaches the network.
    """
    guilds, invited = await _who_this_library_serves(library_id)
    # Once per call, not once per draft. Two drafts of one cube used to mean two
    # identical uncached CubeCobra reads, and library_signup_note runs this
    # inline during draft creation inside a one-second budget.
    read = _read_once(fetch or fetch_cube)

    held = await library_holdings(library_id)
    since = await _reserved_since(exclude_loan_id)
    holds = [await _on_loan(library_id, exclude_loan_id=exclude_loan_id,
                            yield_after=since),
             await _being_drafted(read, library_id, guilds, invited,
                                  yield_after=since)]
    if exclude_loan_id is None:
        holds.append(await _requested_at_signup(read, library_id, guilds, invited))

    spoken_for: "dict[str, int]" = {}
    for hold in holds:
        for name, qty in hold.items():
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


def _read_once(fetch: "Callable[[str], Any]") -> "Callable[[str], Any]":
    """`fetch`, but each cube is read at most once.

    Scoped to one library_available call, so nothing is cached across requests
    and a cube edited between them is seen. Within a call the answer has to be
    the same for every draft anyway -- two drafts of one cube that disagreed
    about its contents would make the arithmetic below meaningless.
    """
    seen: "dict[str, Any]" = {}

    async def read(cube_id: str):
        key = str(cube_id)
        if key not in seen:
            seen[key] = await fetch(key)
        return seen[key]

    return read


async def _who_this_library_serves(library_id: Any) -> "tuple[list[str], set[str]]":
    """The guilds this library lends into, and whoever it is restricted to.

    Both whole-cube holds need both, and resolving them here rather than inside
    each one is what stops a single availability read asking the same two
    questions twice.

    An empty invite set means communal: the library lends to everyone in those
    guilds, so no draft is filtered out for who is in it.
    """
    async with AsyncSessionLocal() as session:
        guilds = [str(g) for g in (await session.scalars(
            select(LibraryServer.guild_id).where(
                LibraryServer.library_id == str(library_id)))).all()]
    if not guilds:
        return [], set()
    return guilds, set(await members(library_id))


async def _whole_cubes(drafts: "list[Any]", read: "Callable[[str], Any]"
                       ) -> "dict[str, int]":
    """Every card in these drafts' cubes, summed: {name: copies}.

    Shared by both whole-cube holds -- the one a draft takes at sign-up and the
    one it keeps while dealing packs -- because they hold the same thing on the
    same terms and differ only in which drafts qualify. Written twice, the
    naming rule below was applied to one of them.

    Through `cube_as_the_library_sees_it`, so what a draft is holding is named
    the way custody is. Comparing a raw CubeCobra list to the ledger meant a
    Universes Beyond card reserved nothing -- its cube name never matched the
    MTGO name it was booked under -- and those copies stayed lendable while a
    draft had them on the table.

    The cubes are read concurrently: these are HTTP reads of up to 30 s each
    (helpers.cube_list), and three held cubes read one after another is three
    times the latency on a path with a one-second budget.
    """
    lists = await asyncio.gather(*(cube_as_the_library_sees_it(d.cube, fetch=read)
                                   for d in drafts))
    committed: "dict[str, int]" = {}
    for draft, seen in zip(drafts, lists):
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
        _tally(cards, into=committed)
    return committed


def _tally(cards: "list[dict[str, Any]]", *, into: "Optional[dict[str, int]]" = None
           ) -> "dict[str, int]":
    """Sum a card list by name into `into`: {name: copies}.

    One adder for every place that counts cards, because each of them is
    summing the same shape and getting the qty default wrong in one of them
    would silently under-count a hold rather than fail.
    """
    out = {} if into is None else into
    for card in cards:
        name = card.get("name")
        if name:
            out[name] = out.get(name, 0) + int(card.get("qty") or 0)
    return out


async def _requested_at_signup(read: "Callable[[str], Any]", library_id: Any,
                               guilds: "list[str]", invited: "set[str]",
                               ) -> "dict[str, int]":
    """The cubes of this library's drafts that are still filling and have
    somebody asking for library cards.

    The whole cube, for the same reason _being_drafted holds the whole cube: what
    a requester will end up drafting is unknowable until the packs are dealt, so
    anything less is a promise the shelf cannot keep. One requester holds all of
    it; several in one draft share it, because they draft from the same copy.

    Only drafts STILL IN SIGN-UP. Once teams form, _being_drafted holds the cube
    on the same terms and this must stop, or one draft would be counted twice.

    An ACTIVE requester is one who is still in sign_ups. Nothing has to listen
    for somebody leaving the queue: they drop out of sign_ups, the intersection
    empties, and the hold is simply no longer computed. A draft whose requesters
    have all gone holds nothing without anybody telling it so.

    And -- where the library is invite-only -- only requesters who could actually
    borrow. Holding a cube for somebody the library would refuse only blocks the
    people it would not.

    A QUEUE CLEANUP HAS REAPED HOLDS NOTHING, and this is the only hold that
    needs saying so. The other two end when a draft produces loans or runs out
    of DRAFTING_WINDOW; this one would end when its requesters left, except that
    nobody leaves a dead queue -- they stop coming back. What ends it is the
    queue's own inactivity deadline, which every sign-up pushes back and which
    cleanup deletes the row at (helpers.stale_drafts.rooms_reaped, shared with
    stake_funding, which drops a dead draft's claim on a stake for the same
    reason).
    """
    if not guilds:
        return {}
    async with AsyncSessionLocal() as session:
        filling = list((await session.scalars(
            select(DraftSession).where(
                DraftSession.teams_start_time.is_(None),
                DraftSession.cube.isnot(None),
                DraftSession.guild_id.in_(guilds),
                # Asked in SQL because it is the selective one: almost no draft
                # has anybody requesting, and without it every availability read
                # loads every queue the guild has ever opened.
                DraftSession.library_requests.isnot(None),
            ))).all())
    if not filling:
        return {}

    now = datetime.now()
    held = []
    for draft in filling:
        if draft.session_stage in FINISHED_STAGES or rooms_reaped(draft, now):
            continue                      # over, or already due to be deleted
        asked = active_requesters(draft)
        if not asked:
            continue                      # everybody who asked has left
        if invited and not invited & asked:
            continue                      # nobody asking could collect a deck
        held.append(draft)
    return await _whole_cubes(held, read)


async def _teams_formed(session: Any, draft_ids: "list[str]") -> "dict[str, datetime]":
    """When each of these drafts formed its teams -- the moment it reserved."""
    if not draft_ids:
        return {}
    rows = (await session.execute(
        select(DraftSession.session_id, DraftSession.teams_start_time).where(
            DraftSession.session_id.in_(draft_ids)))).all()
    return {sid: formed for sid, formed in rows if formed is not None}


async def _reserved_since(loan_id: Optional[int]) -> Optional[datetime]:
    """When this loan's claim on the shelf began, or None if unknown.

    A deck carries its draft's reservation forward: the draft held the cube
    from the moment its teams formed, and assigning decks narrowed that hold,
    it did not start a new one. Dating a deck from its own assignment would
    hand a draft that finished first to one still dealing packs. A loan from no
    draft dates from its creation.
    """
    if loan_id is None:
        return None
    async with AsyncSessionLocal() as session:
        loan = await session.get(CardLoan, loan_id)
        if loan is None:
            return None
        draft_id = session_of(loan)
        if draft_id is None:
            return loan.created_at
        return (await _teams_formed(session, [draft_id])).get(draft_id)


async def _on_loan(library_id: Any, *, exclude_loan_id: Optional[int] = None,
                   yield_after: Optional[datetime] = None) -> "dict[str, int]":
    """Cards committed by THIS library's loans: assigned, in flight, out, or
    coming back. Another library's loans draw down its own shelf, not this
    one's, however much the two share an MTGO account.

    An assigned deck whose draft formed after `yield_after` is a later
    reservation and is left out. Only an ASSIGNED one: anything past that has
    cards in flight or in somebody's hands, and priority cannot take them back.
    """
    async with AsyncSessionLocal() as session:
        loans = list((await session.scalars(
            select(CardLoan).where(
                CardLoan.state.in_(SPOKEN_FOR),
                CardLoan.library_id == str(library_id)))).all())
        formed: "dict[str, datetime]" = {}
        if yield_after is not None:
            formed = await _teams_formed(session, [
                d for d in (session_of(loan) for loan in loans
                            if loan.state == "assigned") if d])

    out: "dict[str, int]" = {}
    for loan in loans:
        if loan.id == exclude_loan_id and loan.state == "assigned":
            continue  # Collecting this reservation must not subtract it twice.
        if loan.state == "assigned" and _later(formed.get(session_of(loan) or ""), yield_after):
            continue  # Reserved after the loan asking; it waits its turn.
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
        _tally(cards or [], into=out)
    return out


def _later(formed: Optional[datetime], than: Optional[datetime]) -> bool:
    """Strictly later. An unknown or tied time does not yield: holding is the
    safe way to be wrong, since the borrower is told what is short."""
    return formed is not None and than is not None and formed > than


async def _being_drafted(read: "Callable[[str], Any]", library_id: Any,
                         guilds: "list[str]", invited: "set[str]", *,
                         yield_after: Optional[datetime] = None
                         ) -> "dict[str, int]":
    """The cubes of THIS library's drafts that are underway but not yet assigned.

    Between teams forming and decks being assigned the packs are dealt but the
    pools are not recorded anywhere, so there is no way to say which cards went
    to whom -- the whole cube is at risk rather than the part that happens to
    have been borrowed. Once any loan exists for the draft, the assignment is
    the precise answer and replaces this one.

    And only drafts somebody in could borrow from. In an invite-only library a
    table of uninvited players will never collect a deck, so holding the cube
    for them only blocks the people who can. One invited drafter is enough to
    hold all of it: which cards they will end up with is unknown until now.

    A draft whose teams formed after `yield_after` holds nothing: see
    library_available.
    """
    if not guilds:
        return {}
    cutoff = datetime.now() - DRAFTING_WINDOW
    async with AsyncSessionLocal() as session:
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

    held = []
    for draft in underway:
        if draft.session_stage in FINISHED_STAGES:
            continue
        if f"draft:{draft.session_id}" in assigned:
            continue                      # its loans speak for it now
        if _later(draft.teams_start_time, yield_after):
            continue                      # reserved after the loan asking
        if invited and not invited & set(draft.sign_ups or {}):
            continue                      # nobody here could collect a deck
        held.append(draft)
    return await _whole_cubes(held, read)


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


async def cube_coverage(library_id: Any, cube_id: Any,
                        fetch: "Optional[Callable[[str], Any]]" = None
                        ) -> "Optional[Support]":
    """Can this library cover a draft of this cube right now?

    THE answer to "can I borrow a deck from this cube", asked in one place
    because three surfaces ask it and they must not diverge: the signup board
    (cube_views.pack_options.library_signup_note), /library request
    (services.library_request_service), and the cube dropdown's badges. Each
    used to walk the same four steps itself -- offers, read the cube, read
    availability, compare -- and a change to any of them reached one caller.

    None means there is nothing to answer, which is NOT a no: no library, a
    cube this one does not lend for, or a cube that could not be read. Folding
    those into "no" tells a player the shelf is busy when the truth is that the
    shelf was never asked, and "try later" is advice that cannot work.
    """
    from services.library_service import offers

    if not library_id or not cube_id:
        return None
    if not await offers(library_id, cube_id):
        return None
    read = _read_once(fetch or fetch_cube)
    seen = await cube_as_the_library_sees_it(cube_id, fetch=read)
    cards = seen.cards if seen else None
    if not cards:
        logger.warning("library: cube {} could not be read; not answering "
                       "whether it is covered", cube_id)
        return None
    return cube_support(cards, await library_available(library_id, fetch=read))
