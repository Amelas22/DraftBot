"""Collecting a deck from the card library, and handing it back.

The two flows behind `/library borrow` and `/library return`, written against
a small `LibraryCaller` rather than a slash command's context, so anything a
player can press -- a command, a button in the draft chat -- runs the same
steps, says the same things, and refuses for the same reasons. The adapters
that turn a pycord context or interaction into a caller live in
`library_views.callers`; this module never sees either.

The bot cannot finish an MTGO trade on its own: it offers, and the player has
to accept in the client. So every dispatched message here says so. A player who
is not told will run the command, see nothing happen, and the job will sit for
its ten-minute wait and fail -- which looks like a broken bot rather than an
unaccepted trade.
"""
from functools import partial
from typing import Any, Awaitable, Callable, Optional, Protocol

import discord
from loguru import logger

from helpers.money_gate import (
    custodian_name, explain_trade_failure, full_trade_list_advice,
    mtgo_job_footer, mtgo_trade_prompt, spawn_followup,
)
from library_views.take_what_is_there import TakeWhatIsThereView
from services.card_lending_service import (
    active_loan,
    borrow_when_free,
    card_lines,
    decline_offer,
    hold_shortfall,
    library_busy_reason,
    poll_until_settled,
    return_when_free,
    shortfall,
)
from services.draft_deck_assignment import (
    RequestOutcome, assign_on_request, latest_draft_of,
)
from services.library_access_service import may_borrow, why_not
from services.library_service import library_id_for
from services.mtgo_tradebot_client import get_lending_client, max_cards_per_trade


class LibraryCaller(Protocol):
    """Whoever asked: what the two flows need to know about them, and the one
    private channel they are answered on.

    `guild_id` is None outside a server the bot can see. That is a real case,
    not a typing artefact -- the commands are reachable from a DM -- so it is
    checked once, by the gate both flows open with, rather than assumed away
    at the boundary.

    Every reply is private: `defer` acknowledges ephemerally, and `send` is an
    ephemeral followup.
    """

    @property
    def user_id(self) -> int: ...

    @property
    def guild_id(self) -> Optional[int]: ...

    async def defer(self) -> None: ...

    async def send(self, content: str, *,
                   view: Optional[discord.ui.View] = None) -> None: ...


# Nothing about the trade, so nothing that depends on which way it was going:
# an unlinked account and an unreachable serve are the same fact to a borrower
# and to a depositor. Shared because two copies of a setup instruction drift,
# and a player who follows the stale half is being sent somewhere that does
# not fix their problem.
SETUP_MESSAGES = {
    "not_linked": "🔗 Link your MTGO account first with `/link_mtgo <username>`, so the "
                  "library knows who to trade with.",
    "unavailable": "🔌 The card library is unavailable right now. Try again shortly.",
}

# Why a drafter who asked for their deck after the draft was not handed one
# (draft_deck_assignment.assign_on_request). "not_allowed" is answered by
# why_not instead, which can say what the library's rule is.
_CANNOT_COLLECT = "⚠️ The library can't put your deck together for that draft — ask an admin."
REQUEST_MESSAGES: "dict[RequestOutcome, str]" = {
    "not_offered": "📭 The library doesn't lend that draft's cube.",
    "already": "📭 You've already had your deck from that draft.",
    "over": "📭 Your matches in that draft are done, so the library can't lend you "
            "its deck any more.",
    "busy": "📦 You're still holding a library deck from another draft — hand it back "
            "with `/library return` first.",
    "no_log": "⏳ That draft's picks aren't in yet — try again in a minute.",
    "unseatable": _CANNOT_COLLECT,
    "empty": _CANNOT_COLLECT,
    "too_large": _CANNOT_COLLECT,
    "failed": _CANNOT_COLLECT,
}

# Two tables, not one, for everything else. The same status means opposite
# things depending on which way the cards were going: a borrow that never
# dispatched has a hold to give back, a card deposit that never dispatched has
# nothing to undo. Folding those together would have to pick one wording for
# both, and the wrong half of every pair would then be telling the player
# something untrue. (The custody table lives with the deposit commands.)
LOAN_MESSAGES: "dict[str, str | Callable[[], str]]" = {
    **SETUP_MESSAGES,
    "no_loan": "📭 The library has no deck waiting for you.",
    "already_in_flight": "⏳ That trade is already open — accept it in MTGO.",
    "already_borrowed": "📦 You already have your deck. Use `/library return` "
                        "when you're done with it.",
    "not_borrowed": "📭 You don't have a deck out from the library.",
    "no_wallet": "💸 This library asks for a tix hold, but the wallet isn't enabled "
                 "on this server. Ask an admin to sort one or the other.",
    "still_busy": "🕑 The library is still busy after a long wait. Nothing has moved and "
                  "nothing has been charged — try again shortly.",
    # Normally rendered by describe_hold_shortfall, which has the real
    # figures. This is the fallback for when the wallet cannot be read.
    "short_funds": "💰 You don't have enough tix to cover the hold on this deck. "
                   "Top up with `/wallet` and try again — nothing has been charged.",
    "short_cards": "📦 This library can no longer cover that offer. Run "
                   "`/library borrow` again to see what's available.",
    "not_invited": "🔒 This library doesn't lend to you. Run `/library borrow` again to see why.",
    "wrong_server": "📍 Your deck is waiting in the server you drafted it in — run "
                    "`/library borrow` there. (`/library return` works from anywhere.)",
    # A callable, not a string: the limit is read from the environment when
    # asked, so baking it into this table at import time would freeze whatever
    # value happened to be loaded first.
    "too_large": lambda: (f"📦 That deck is bigger than MTGO will move in one trade "
                          f"({max_cards_per_trade()} cards). Ask an admin — the "
                          f"library can't hand over a deck this size yet."),
    "dispatch_failed": "⚠️ MTGO didn't accept the trade request. Your hold is back "
                       "— try again in a minute.",
    # Deliberately does NOT invite a retry: the request may have reached MTGO
    # and opened a real trade, and a second one would hand out a second deck.
    "dispatch_unknown": "⚠️ We lost contact with MTGO while setting up the trade, so we can't "
                        "tell whether it started. Check MTGO for a message from the library "
                        "bot — if there isn't one, ask an admin to sort out your hold.",
}


def loan_said(status: str, fallback: str) -> str:
    """The line for one status. Entries may be callables, for the ones whose
    text depends on configuration read at call time rather than at import."""
    text = LOAN_MESSAGES.get(status, fallback)
    return text() if callable(text) else text


def describe_hold_shortfall(figures: "dict[str, int]") -> str:
    """Why the borrow could not be paid for, in the numbers the player needs.

    All of them: the hold this deck carries, the week's pass where one is
    owed, what their wallet holds, and the difference. Without the gap a player
    cannot tell whether they are one tix short or ten, so the only way forward
    is to top up blind and retry until it works -- which is the same dead end
    /library borrow used to be when the library was short of cards.

    """
    cost = f"a **{figures['hold']} tix** hold"
    return (f"💰 Borrowing this deck needs {cost}, and your wallet has "
            f"**{figures['have']}**. Add **{figures['short']}** more with "
            f"`/wallet` and run the command again — nothing has been charged.")


def describe_shortfall(short: "list[dict[str, Any]]") -> str:
    """Name what the library is short, and by how much.

    The number they have to find elsewhere is the actionable part -- "not
    enough Swamps" leaves a player guessing whether that means one or nine.
    """
    if not short:
        return ""
    lines: "list[str]" = []
    for item in short:
        name, want, have = item["name"], item["want"], item["have"]
        if have <= 0:
            lines.append(f"• **{name}** — none available (you need {want})")
        else:
            lines.append(f"• **{name}** — only **{have}** of {want} available; "
                         f"you'd need to source the other **{want - have}**")
    return within_a_message(lines)


# Discord refuses a message over 2000 characters, and the send RAISES -- so an
# over-long shortfall does not get truncated, it gets no reply at all. Every
# loan used to be a hand-seeded fixture of two or three cards; a drafted pool is
# up to 48 distinct names, and a library that covers none of them names all 48
# at ~60 characters each. The budget leaves room for the surrounding message.
_SHORTFALL_BUDGET = 1500


def within_a_message(lines: "list[str]") -> str:
    """As many lines as will fit, then a count of what did not.

    Listing the first N is the useful half: the player is going to source these
    themselves, and a truncated list they can act on beats an exception they
    never see.
    """
    kept: "list[str]" = []
    used = 0
    for i, line in enumerate(lines):
        remaining = len(lines) - i
        tail = f"\n…and **{remaining}** more" if remaining else ""
        if used + len(line) + 1 + len(tail) > _SHORTFALL_BUDGET:
            kept.append(f"…and **{remaining}** more")
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)


def deck_lines(loan: Any) -> str:
    # The pending offer in preference to the deck, exactly as the dispatch reads
    # it: a borrower who took a partial deck is sent a trade for the partial
    # deck, and listing the whole one beside it invites them to reject a window
    # that is "missing" cards they were told to expect.
    cards = getattr(loan, "pending_cards", None) or getattr(loan, "cards", None)
    if not cards:            # also covers loan being None
        return ""
    return card_lines(cards)


OUTSIDE_A_SERVER = "The card library can only be used in a server."


def unusable_reason(guild_id: Optional[int]) -> Optional[str]:
    """Why the card library cannot be used from here, or None.

    `guild_id` is None outside a server the bot can see.

    Deliberately says nothing about PRICE. What a deck costs is a property of
    the cube it was drafted from, and this runs before any cube is known -- it
    guards /library deck and /library return too, which have no cube until a loan is looked
    up. A guild-wide collateral used to be read here and is not any more: it
    made a free cube demand a wallet, because the number it consulted had
    stopped being the one that decides.

    The wallet requirement moved to where the charge actually happens, which is
    the only place that knows whether this particular cube costs anything.
    """
    if guild_id is None:
        return OUTSIDE_A_SERVER
    # Checked HERE rather than left to the dispatch, which is behind the queue:
    # a disabled client makes vault() answer None (so nothing reads as short)
    # and the busy check read as "can't reach the custodian", so the borrower
    # was told they were next, waited out the five-minute queue and got "try
    # again shortly" -- an invitation to repeat it. Mirrors gate_serve.
    if not get_lending_client().enabled:
        return ("The card library isn't configured on this bot "
                "(set MTGO_LENDING_URL and MTGO_LENDING_TOKEN).")
    return None


async def open_if_usable(caller: LibraryCaller) -> Optional[int]:
    """Answer the interaction, and give back the server it can trade in.

    None, having already said why, where the library cannot be used. Every
    library command that trades opens this way -- these two flows here, and
    /library deck, deposit and withdraw through the cog's `defer_if_usable` --
    so a server that is not set up is told so in one voice, and so the defer
    cannot be forgotten in one of them, which turns a clear refusal into a
    silent "application did not respond".
    """
    await caller.defer()
    guild_id = caller.guild_id
    blocked = unusable_reason(guild_id)
    if blocked:
        await caller.send(blocked)
        return None
    return guild_id


async def _ask_for_deck(caller: LibraryCaller, guild_id: int,
                        session_id: Optional[str]) -> bool:
    """For a caller with no deck waiting: ask for their pool from `session_id`,
    or from the last draft they played in this server. False, having said why,
    if it was not handed over; True if it was, or if there is no draft to ask
    about -- the borrow then carries on and says what it always said."""
    if session_id is None:
        session_id = await latest_draft_of(guild_id, caller.user_id)
        if session_id is None:
            return True
    outcome = await assign_on_request(session_id, caller.user_id)
    if outcome in ("assigned", "not_drafter"):
        return True
    if outcome == "not_allowed":
        library_id = await library_id_for(guild_id)
        await caller.send(await why_not(library_id, caller.user_id))
    else:
        await caller.send(REQUEST_MESSAGES.get(outcome, _CANNOT_COLLECT))
    return False


async def borrow(caller: LibraryCaller, session_id: Optional[str] = None) -> None:
    """Collect the deck the library is holding for this caller -- asking for it
    first if there is none, so a drafter who said no before the draft, or
    turned the offer down, can still have it (draft_deck_assignment.
    assign_on_request). `session_id` is the draft whose panel was pressed;
    the command asks of the caller's latest draft."""
    guild_id = await open_if_usable(caller)
    if guild_id is None:
        return

    # Only borrowing is gated. Depositing and withdrawing stay open: somebody
    # contributing cards is not the risk, and a sponsor locked out of their
    # own deposits would be absurd.
    # Asked of the LIBRARY, not the server: somebody trusted with a
    # sponsor's cards is trusted with them in every room it lends into.
    library_id = await library_id_for(guild_id)
    if library_id is None:
        await caller.send("📭 This server isn't set up to borrow from a card library.")
        return
    loan = await active_loan(caller.user_id)
    if loan is None:
        if not await _ask_for_deck(caller, guild_id, session_id):
            return
        loan = await active_loan(caller.user_id)
    if loan is not None:
        library_id = getattr(loan, "library_id", None) or library_id
    if not await may_borrow(library_id, caller.user_id):
        await caller.send(await why_not(library_id, caller.user_id))
        return

    # Can the library actually cover this deck? Asked before queueing, so a
    # shortfall is reported now rather than surfacing minutes later as a
    # failed MTGO trade the player was told to go and accept.
    if loan is not None and loan.state == "assigned":
        short = await shortfall(guild_id, loan.cards or [], loan.id)
        if short:
            view = TakeWhatIsThereView(guild_id, loan.id,
                                       partial(_borrow_when_ready, caller, guild_id))
            await caller.send(
                f"📦 The library can't cover your whole deck right now:\n"
                f"{describe_shortfall(short)}\n\n"
                f"You can take what's there and source the rest yourself — "
                f"your `/library return` will only ask for what you actually borrowed.",
                view=view)
            return

    await _warn_if_busy(caller)
    spawn_followup("card-library borrow", _borrow_when_ready(caller, guild_id))


async def decline(caller: LibraryCaller, session_id: str) -> None:
    """Hand back the deck this draft offered the caller, before collecting it,
    so its cards go back on the shelf and their loan slot is free now rather
    than when the offer would have lapsed. Moves no cards, so it needs no
    serve and opens no trade."""
    await caller.defer()
    if await decline_offer(caller.user_id, session_id):
        await caller.send(
            "👍 Handed back — those cards are back on the shelf. Changed your mind? "
            "Press **Borrow my deck** while you still have a match to play.")
    else:
        await caller.send(
            "📭 You have no deck waiting from this draft. If it's on its way to you "
            "or in your hands already, hand it back with **Return my deck**.")


async def return_deck(caller: LibraryCaller) -> None:
    """Hand back the deck this caller borrowed."""
    guild_id = await open_if_usable(caller)
    if guild_id is None:
        return
    await _warn_if_busy(caller)
    spawn_followup("card-library return", _trade_when_ready(
        caller, guild_id, return_when_free, "return", "returned"))


async def _warn_if_busy(caller: LibraryCaller) -> None:
    """The library trades with one person at a time. Say so up front rather
    than opening a trade window that will not appear for minutes, and keep
    their place in line instead of asking them to come back."""
    if await library_busy_reason():
        await caller.send(
            "🕑 The library is trading with someone else right now — you're next. "
            "I'll message you the moment your trade is ready, so you can stay put.")


def _borrow_when_ready(caller: LibraryCaller, guild_id: int,
                       offering: "Optional[list[dict[str, Any]]]" = None
                       ) -> "Awaitable[str]":
    return _trade_when_ready(
        caller, guild_id, partial(borrow_when_free, offering=offering),
        "collect", "borrowed",
        note="\n⚠️ When you're done, **move these cards into your MTGO trade "
             "binder** before `/library return` — the bot can only take back what's "
             "in your binder.",
        offering=offering)


async def _trade_when_ready(caller: LibraryCaller, guild_id: int,
                            when_free: "Callable[[int, int], Awaitable[tuple[str, bool]]]",
                            verb: str, expect: str, note: str = "",
                            offering: "Optional[list[dict[str, Any]]]" = None
                            ) -> str:
    """Wait for the library's turn, dispatch, then see the trade through.

    Returns the dispatch status ("dispatched", "short_funds", ...) so a caller
    that cares can tell how it went; one that ignores it is unaffected.
    `offering` is the cards being sent, so a short-funds answer quotes the hold
    for THAT trade rather than for the whole deck.

    Runs detached so the interaction is not held open for the wait: Discord
    gives a command 15 minutes of followups, but a player staring at a
    spinner for five of them will assume it broke.
    """
    status, waited = await when_free(guild_id, caller.user_id)
    if status == "short_funds":
        # The figures are a nicety; answering at all is not. Rendering them
        # costs a wallet read, so unlike the static strings this branch can
        # raise -- and a raise here leaves the interaction deferred and
        # never answered, which Discord shows as "did not respond".
        try:
            figures = await hold_shortfall(guild_id, caller.user_id,
                                           offering=offering)
            said = (describe_hold_shortfall(figures) if figures
                    else loan_said(status, status))
        except Exception:
            logger.exception("library: could not read the hold figures for {}",
                             caller.user_id)
            said = loan_said(status, status)
        await caller.send(said)
        return status
    if status != "dispatched":
        await caller.send(loan_said(status, f"⚠️ Couldn't {verb} your deck ({status})."))
        return status

    loan = await active_loan(caller.user_id)
    who = await custodian_name(get_lending_client())
    lead = "✅ **Your turn.** " if waited else ""
    job_id = getattr(loan, "job_id", None)
    await caller.send(
        f"{lead}🤝 **Your deck is ready to {verb}.** {mtgo_trade_prompt(who)}\n"
        # Only when the bot is TAKING cards. Collecting a deck gives cards
        # to the player, who offers nothing and needs no advice about how.
        f"{full_trade_list_advice(exact_printing=True) if expect == 'returned' else ''}\n\n"
        f"{deck_lines(loan)}"
        f"{mtgo_job_footer(job_id) if job_id else ''}{note}")

    # Wait on the job so the player gets a real answer rather than silence.
    # The watchdog covers anything that outlives this poll.
    outcome, detail = await poll_until_settled(guild_id, caller.user_id, expect,
                                               job_id=job_id)
    await _report_outcome(caller, outcome, expect, detail)
    return status


async def _report_outcome(caller: LibraryCaller, outcome: str, expected: str,
                          detail: Optional[str] = None) -> None:
    """Say what happened, and for a failure, what to do about it.

    A failed borrow and a failed return need opposite advice: the first
    leaves the deck on the shelf, the second leaves it with the borrower.
    Both carry the serve's own reason, which is usually the only actionable
    part -- a short binder or a mistyped username is the player's to fix.
    """
    if outcome == "borrowed":
        await caller.send(
            "✅ Deck collected — it's in your MTGO account.\n"
            "_Move it into your **trade binder** before `/library return`, or the bot "
            "can't take it back._")
    elif outcome == "returned":
        await caller.send("✅ Deck returned. Thanks!")
    elif outcome == "failed":
        why = explain_trade_failure(detail or "the trade didn't complete")
        if expected == "returned":
            await caller.send(
                f"❌ The deck didn't come back, so it's **still with you** and still "
                f"on loan.\n\n{why}\n\nRun `/library return` again when it's sorted.")
        else:
            await caller.send(
                f"❌ The trade didn't complete, so no cards moved — your deck is "
                f"**still reserved** and your hold is back.\n\n{why}\n\n"
                f"Run `/library borrow` again when you're ready — it costs no more.")
    # still running: the watchdog will settle it; saying nothing is correct
