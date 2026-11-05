"""Offering an absent teammate's drafted pool to a sub.

A pool is lent to one person at a time. Everything here is about keeping it
that way: the absent player must not hold it, the sub must hold nothing (their
own pool stays theirs to ask for later), and no other sub may have it.

Lock contract: neither `check_sub` nor `offer_to_sub` takes the draft's panel
lock, and neither awaits a panel sync. The caller wraps both in
`async with library_panel._lock_for(session_id):` and calls
`library_panel.nudge(bot, session_id)` after releasing it -- the lock is not
re-entrant, so a sync awaited from inside would deadlock. `offer_to_sub`
re-runs the two pool checks because it is what runs under that lock last: two
Confirms for one pool queue on it, and the second finds the first's loan.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from services.card_lending_service import (
    REQUEST_GRACE, active_loan, assign_deck, decline_offer,
    sub_loan_for)
from services.card_library_inventory import library_available
from services.draft_deck_assignment import (
    _cube_of, _pool_of, _session_meta, loan_source, too_large)
from services.library_access_service import may_borrow_many, why_not
from services.library_loan_actions import REQUEST_MESSAGES
from services.library_panel_state import OUT_STATES
from services.library_reminders import draft_state, may_still_request
from services.library_service import library_for, offers


@dataclass(frozen=True)
class SubContext:
    session_id: str
    guild_id: str
    absent_id: str
    absent_name: str
    sub_id: str
    sub_name: str           # draft name, else the member's display name
    sub_in_draft: bool      # in sign_ups
    team_key: Optional[str]  # "A"/"B" of the absent player, None if team-less


def _name(sign_ups: "dict[str, Any]", player_id: Any) -> str:
    return str(sign_ups.get(str(player_id)) or f"<@{player_id}>")


def _ids(team: Any) -> "set[str]":
    return {str(p) for p in (team or [])}


async def sub_context(session_id: str, absent_id: str, sub_id: str,
                      sub_display_name: str) -> SubContext:
    """Who is subbing for whom in this draft, read from its row once."""
    async with AsyncSessionLocal() as session:
        row = await session.scalar(
            select(DraftSession).where(DraftSession.session_id == str(session_id)))
    if row is None:
        raise ValueError(f"no draft {session_id}")
    sign_ups = {str(k): v for k, v in (row.sign_ups or {}).items()}
    absent, sub = str(absent_id), str(sub_id)
    team_key = ("A" if absent in _ids(row.team_a)
                else "B" if absent in _ids(row.team_b) else None)
    return SubContext(
        session_id=str(session_id), guild_id=str(row.guild_id),
        absent_id=absent, absent_name=_name(sign_ups, absent),
        sub_id=sub, sub_name=str(sign_ups.get(sub) or sub_display_name),
        sub_in_draft=sub in sign_ups, team_key=team_key)


def _absent_holds_text(ctx: SubContext) -> str:
    return (f"📭 **{ctx.absent_name}** still has their deck out. "
            "It has to come back before a sub can take it.")


def _sub_busy_text(ctx: SubContext) -> str:
    return (f"📭 **{ctx.sub_name}** still has a deck out. Press **Return my deck** "
            "first, then **Sub for a player** again. Their own pool stays theirs "
            "to borrow back later.")


def _cannot_build_text(ctx: SubContext) -> str:
    return f"⚠️ The library can't put **{ctx.absent_name}**'s deck together — ask an admin."


async def _with_another_sub_text(ctx: SubContext) -> Optional[str]:
    """Refusal if the absent player's pool is already on offer or out to a sub."""
    loan = await sub_loan_for(loan_source(ctx.session_id), ctx.absent_id)
    if loan is None:
        return None
    meta = await _session_meta(ctx.session_id)
    sign_ups: "dict[str, Any]" = (
        {str(k): v for k, v in meta[1].items()} if meta is not None else {})
    return (f"📭 **{_name(sign_ups, loan.borrower_id)}** already has "
            f"**{ctx.absent_name}**'s deck as their sub. It has to come back first.")


async def _absent_holds(ctx: SubContext) -> "tuple[Optional[str], Optional[int]]":
    """Refusal if the absent player has this draft's deck out, and the id of
    their own uncollected offer from this draft, if any -- that offer is
    retracted when the sub's is made, so it does not count against the shelf."""
    own = await active_loan(ctx.absent_id)
    if own is None or own.source != loan_source(ctx.session_id):
        return None, None
    if own.state in OUT_STATES:
        return _absent_holds_text(ctx), None
    return None, (own.id if own.state == "assigned" else None)


async def _lendable_pool(ctx: SubContext
                         ) -> "tuple[Optional[str], list[dict[str, Any]], Any]":
    """(refusal, the absent player's pool, the library) for the shelf-side
    checks: the cube is offered, the pool can be read, and it is not too large."""
    meta = await _session_meta(ctx.session_id)
    if meta is None:
        return _cannot_build_text(ctx), [], None
    guild_id, sign_ups = meta
    cube = await _cube_of(ctx.session_id)
    library = await library_for(guild_id)
    if cube is None or library is None or not await offers(library.id, cube):
        return REQUEST_MESSAGES["not_offered"], [], library
    key = next((k for k in sign_ups if str(k) == ctx.absent_id), None)
    if key is None:
        return _cannot_build_text(ctx), [], library
    cards = await _pool_of(ctx.session_id, key, sign_ups)
    if cards == "no_log":
        return REQUEST_MESSAGES["no_log"], [], library
    if isinstance(cards, str) or not cards \
            or too_large(sum(int(c["qty"]) for c in cards)):
        return _cannot_build_text(ctx), [], library
    return None, cards, library


async def check_sub(ctx: SubContext) -> Optional[str]:
    """Why this sub may not take the absent player's pool, or None if they may.
    Writes nothing. Run under the draft's panel lock (see the module docstring)."""
    refused, own_offer = await _absent_holds(ctx)
    if refused is not None:
        return refused

    held = await active_loan(ctx.sub_id)
    if held is not None:
        if held.state == "assigned" and held.source == loan_source(ctx.session_id):
            return (f"📭 **{ctx.sub_name}** has their own deck waiting. Press "
                    "**I don't need my deck** first, then **Sub for a player** "
                    "again; the pool stays theirs to ask for later.")
        return _sub_busy_text(ctx)

    refused = await _with_another_sub_text(ctx)
    if refused is not None:
        return refused

    async with AsyncSessionLocal() as session:
        row = await session.scalar(
            select(DraftSession).where(DraftSession.session_id == ctx.session_id))
    other = ({"A": _ids(row.team_b), "B": _ids(row.team_a)}.get(ctx.team_key or "", set())
             if row is not None else set())
    if ctx.sub_id in other:
        return f"📭 **{ctx.sub_name}** plays for the other team."

    if ctx.sub_id == ctx.absent_id:
        return f"📭 Pick somebody else to sub for **{ctx.absent_name}**."

    library = await library_for(ctx.guild_id)
    if library is not None \
            and ctx.sub_id not in await may_borrow_many(library.id, [ctx.sub_id]):
        return await why_not(library.id, ctx.sub_id)

    late = may_still_request(await draft_state(ctx.session_id), ctx.absent_id)
    if late == "closed":
        return REQUEST_MESSAGES["closed"]
    if late == "over":
        return (f"📭 Every match of **{ctx.absent_name}**'s in this draft is "
                "reported, so there's nothing left to sub for.")

    refused, cards, library = await _lendable_pool(ctx)
    if refused is not None:
        return refused
    avail = await library_available(library.id, exclude_loan_id=own_offer,
                                    exclude_session_id=ctx.session_id)
    if not any(avail.get(c["name"], 0) for c in cards):
        return (f"📭 None of **{ctx.absent_name}**'s deck is on the shelf right "
                "now — try again once the other decks are back.")
    return None


async def offer_to_sub(ctx: SubContext) -> "int | str":
    """Offer the absent player's pool to the sub. Returns the new loan's id, or
    a refusal. Run under the draft's panel lock, after check_sub; never awaits
    a panel sync (see the module docstring).

    The absent player's own uncollected offer is retracted first. `ready_dm_at`
    is left unset so the "deck waiting" DM reaches the sub."""
    refused, _ = await _absent_holds(ctx)
    if refused is None:
        refused = await _with_another_sub_text(ctx)
    if refused is not None:
        return refused
    refused, cards, library = await _lendable_pool(ctx)
    if refused is not None:
        return refused
    await decline_offer(ctx.absent_id, ctx.session_id)
    try:
        loan_id = await assign_deck(
            ctx.guild_id, ctx.sub_id, cards, source=loan_source(ctx.session_id),
            library_id=str(library.id), claimed_at=datetime.now(),
            on_behalf_of=ctx.absent_id)
    except IntegrityError:
        return _sub_busy_text(ctx)
    except Exception:
        logger.exception("library sub: could not offer {}'s pool from {} to {}",
                         ctx.absent_id, ctx.session_id, ctx.sub_id)
        return _cannot_build_text(ctx)
    logger.info("library sub: {} offered {}'s pool from {} (loan {})",
                ctx.sub_id, ctx.absent_id, ctx.session_id, loan_id)
    return loan_id


def chat_line(ctx: SubContext) -> str:
    """What the draft chat is told once the offer is made."""
    return (f"**{ctx.sub_name}** is subbing for **{ctx.absent_name}** and has been "
            f"offered **{ctx.absent_name}**'s deck.")


def confirm_reply(ctx: SubContext) -> str:
    """The private reply to the Confirm press."""
    minutes = int(REQUEST_GRACE.total_seconds() // 60)
    return (f"✅ **{ctx.absent_name}**'s deck is offered to **{ctx.sub_name}**. They "
            "collect it with **Borrow my deck** here or the **Borrow** button in "
            f"their DM; the offer lasts {minutes} minutes.")
