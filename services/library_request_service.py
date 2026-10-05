"""Who asked for library cards for a draft, and whether the shelf can promise them.

The problem this solves: a player could sign up, draft for forty-five minutes,
build a deck, and only then have /library borrow tell them the shelf could not
cover it. Nothing before that moment asked the question, so the answer arrived
after the only point at which it was still useful.

So a player asks AT SIGN-UP, and the answer is given then. Granting a request
holds the cube for that draft (services/card_library_inventory._requested_at_signup),
which is why it can be refused: the shelf has one copy of each card, and a draft
already holding it is a draft that will be short if a second one is promised the
same cards.

Two properties worth stating, because both are load-bearing and neither is obvious:

* An ACTIVE requester is one who is in library_requests AND still in sign_ups.
  Release is therefore DERIVED, not evented -- the last requester leaving the
  queue empties the intersection and the hold stops being computed, with nothing
  listening for a departure and nothing to go stale if a leave path is missed.

* A refusal is not final. The shelf frees up constantly (the Lounge runs
  back-to-back drafts), so a player refused at sign-up may ask again while the
  queue is still filling, and nothing has to remember that they were told no.
"""
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import update

from database.db_session import db_session
from models.draft_session import DraftSession

# card_library_inventory imports active_requesters from here, so coverage()
# defers its own imports of that module to break the cycle. Only the annotation
# needs the name up here.
if TYPE_CHECKING:
    from services.card_library_inventory import Support


def requested_ids(draft: Any) -> "set[str]":
    """Everybody who has ever asked on this draft, whether or not still signed up."""
    return {str(i) for i in (getattr(draft, "library_requests", None) or [])}


def active_requesters(draft: Any) -> "set[str]":
    """Requesters who are still in the queue -- the ones the hold is held for.

    The intersection IS the release mechanism. A requester who leaves the draft
    is removed from sign_ups by the ordinary cancel path, so they fall out of
    here on the next read and the cube stops being held for them. When the last
    one goes the set is empty and _requested_at_signup skips the draft entirely.

    Reading sign_ups rather than trusting library_requests also means a draft
    cannot hold the shelf for somebody who was never in it -- a stale id written
    by a retry or a repair holds nothing.
    """
    signed_up = {str(i) for i in (getattr(draft, "sign_ups", None) or {})}
    return requested_ids(draft) & signed_up


async def _store(draft: Any, ids: "list[str]") -> "set[str]":
    """Store this draft's requests and answer who now holds its cube.

    A fresh list, not a mutation: SQLAlchemy does not see an in-place change to
    a JSON column, so appending would write nothing and the hold would silently
    not exist. (The sign-up paths rebuild sign_ups for the same reason.)

    The answer comes from the list just written rather than from a re-read --
    the caller needs no second query, and a re-read can come back as a draft
    that has meanwhile started.
    """
    async with db_session() as session:
        async with session.begin():
            await session.execute(
                update(DraftSession)
                .where(DraftSession.session_id == draft.session_id)
                .values(library_requests=ids))
    draft.library_requests = ids
    return active_requesters(draft)


async def record_request(draft: Any, user_id: Any) -> "set[str]":
    """Hold this draft's cube for `user_id`, and say who holds it now.

    Granted on the strength of a `coverage` check the caller has already made.
    Nothing locks between the two, so two drafts asking at the same moment can
    both be granted -- the same best-effort this feature accepts everywhere: a
    hold is not proof against a withdrawal either, and the recovery for both is
    the borrow being trimmed to what is actually on the shelf.
    """
    return await _store(draft, sorted(requested_ids(draft) | {str(user_id)}))


async def record_release(draft: Any, user_id: Any) -> "set[str]":
    """Give up `user_id`'s claim, and say who is left holding the cube."""
    return await _store(draft, sorted(requested_ids(draft) - {str(user_id)}))


async def coverage(draft: Any, library_id: Any,
                   fetch: "Optional[Any]" = None) -> "Optional[Support]":
    """Can the shelf promise this draft's cube? A Support, or None.

    None means there is nothing to promise -- no library, a cube it does not
    lend for, or a cube that could not be read -- which must not be folded into
    "no": a player on a cube the library never stocked should be told that, not
    told the shelf is busy.

    Asked against what is AVAILABLE rather than what is held, so a cube already
    promised to another draft reads as short. That is the whole point of asking
    at sign-up: the refusal is what stops two drafts being promised one copy.

    ANOTHER draft. A draft that is already holding its cube answers yes without
    asking, because the availability it would be measured against has its own
    hold subtracted from it -- so the second player at a table would be told the
    cube was in use by the draft they are sitting at. Nothing is promised twice:
    the hold is the whole cube for the whole draft, so a second requester adds
    no demand. `ok` here means "this draft has it", which is the question that
    was asked; what the shelf can physically hand over is settled at /library
    borrow, against the serve, for the first requester and the second alike.
    """
    from services.card_library_inventory import Support, cube_coverage
    from services.library_service import offers

    cube = getattr(draft, "cube", None)
    if not library_id or not cube:
        return None
    if active_requesters(draft):
        # Still None for a cube the library has stopped lending for: that reads
        # as "not stocked", which is a different message from "held".
        return Support(ok=True) if await offers(library_id, cube) else None
    return await cube_coverage(library_id, cube, fetch=fetch)
