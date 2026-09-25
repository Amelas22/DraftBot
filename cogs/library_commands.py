"""The card library: borrowing a deck, and lending your cards to it.

    /library borrow    collect the deck the library is holding for you
    /library return    hand it back
    /library deck      what it has for you, and where it is
    /library deposit   hand a cube's cards over for others to borrow
    /library withdraw  take your cards back
    /library deposits  what it is holding of yours

One cog and one command group, because the six are one feature: /library
deposit is what puts the cards on the shelf that /library borrow hands out,
and they share their gate, their message budget and their view of what the
shelf holds. Split across two cogs, half of that was imported across the
boundary and the other half was written twice.

The bot cannot finish an MTGO trade on its own: it offers, and the player has
to accept in the client. So every dispatched message here says so. A player who
is not told will run the command, see nothing happen, and the job will sit for
its ten-minute wait and fail -- which looks like a broken bot rather than an
unaccepted trade.
"""
import time
from typing import Any, Awaitable, Callable, Optional

import discord
from discord.commands import SlashCommandGroup
from discord.ext import commands
from loguru import logger

from config import bot_config, get_config, is_money_server, save_config
from debt_views.helpers import card_count_label
from helpers.money_gate import (
    custodian_name, explain_trade_failure, full_trade_list_advice,
    mtgo_job_footer, mtgo_trade_prompt,
    spawn_followup,
)
from services.card_deposit_service import (
    chunk_cards, held_for, start_deposit, start_withdrawal,
    withdrawal_orders,
)
from services.card_deposit_service import poll_until_settled as poll_deposit
from services.card_library_inventory import cube_as_the_library_sees_it
from services.card_lending_service import (
    active_loan,
    borrow_when_free,
    deposit_shortfall,
    library_busy_reason,
    poll_until_settled,
    trim_to_available,
    return_when_free,
    shortfall,
)
from services.library_access_service import may_borrow
from services.library_service import (
    is_communal, library_for, library_id_for, offer_cube,
)
from services.mtgo_tradebot_client import get_lending_client, max_cards_per_trade


# Nothing about the trade, so nothing that depends on which way it was going:
# an unlinked account and an unreachable serve are the same fact to a borrower
# and to a depositor. Shared because two copies of a setup instruction drift,
# and a player who follows the stale half is being sent somewhere that does
# not fix their problem.
_SETUP_MESSAGES = {
    "not_linked": "🔗 Link your MTGO account first with `/link_mtgo <username>`, so the "
                  "library knows who to trade with.",
    "unavailable": "🔌 The card library is unavailable right now. Try again shortly.",
}

# Two tables, not one, for everything else. The same status means opposite
# things depending on which way the cards were going: a borrow that never
# dispatched has a deposit to give back, a deposit that never dispatched has
# nothing to undo. Folding those together would have to pick one wording for
# both, and the wrong half of every pair would then be telling the player
# something untrue.
_LOAN_MESSAGES = {
    **_SETUP_MESSAGES,
    "no_loan": "📭 The library has no deck waiting for you.",
    "already_in_flight": "⏳ That trade is already open — accept it in MTGO.",
    "already_borrowed": "📦 You already have your deck. Use `/library return` "
                        "when you're done with it.",
    "not_borrowed": "📭 You don't have a deck out from the library.",
    "no_wallet": "💸 This library asks for a tix deposit, but the wallet isn't enabled "
                 "on this server. Ask an admin to sort one or the other.",
    "still_busy": "🕑 The library is still busy after a long wait. Nothing has moved and "
                  "nothing has been charged — try again shortly.",
    # Normally rendered by describe_deposit_shortfall, which has the real
    # figures. This is the fallback for when the wallet cannot be read.
    "short_funds": "💰 You don't have enough tix to cover the deposit on this deck. "
                   "Top up with `/wallet` and try again — nothing has been charged.",
    "short_cards": "📦 This library can no longer cover that offer. Run "
                   "`/library borrow` again to see what's available.",
    "not_invited": "🔒 You're not on this library's borrowing list. Ask whoever runs it for an invite.",
    "wrong_server": "📍 Your deck is waiting in the server you drafted it in — run "
                    "`/library borrow` there. (`/library return` works from anywhere.)",
    # A callable, not a string: the limit is read from the environment when
    # asked, so baking it into this table at import time would freeze whatever
    # value happened to be loaded first.
    "too_large": lambda: (f"📦 That deck is bigger than MTGO will move in one trade "
                          f"({max_cards_per_trade()} cards). Ask an admin — the "
                          f"library can't hand over a deck this size yet."),
    "dispatch_failed": "⚠️ MTGO didn't accept the trade request. Your deposit is back "
                       "— try again in a minute.",
    # Deliberately does NOT invite a retry: the request may have reached MTGO
    # and opened a real trade, and a second one would hand out a second deck.
    "dispatch_unknown": "⚠️ We lost contact with MTGO while setting up the trade, so we can't "
                        "tell whether it started. Check MTGO for a message from the library "
                        "bot — if there isn't one, ask an admin to sort out your deposit.",
}


def _loan_said(status: str, fallback: str) -> str:
    """The line for one status. Entries may be callables, for the ones whose
    text depends on configuration read at call time rather than at import."""
    text = _LOAN_MESSAGES.get(status, fallback)
    return text() if callable(text) else text


_CUSTODY_MESSAGES = {
    **_SETUP_MESSAGES,
    "nothing_to_deposit": "📭 That cube came back empty — nothing to deposit.",
    "busy": "🕑 The library is trading with someone else right now — try again in a minute.",
    "dispatch_failed": "⚠️ MTGO didn't accept the trade request. Nothing has moved — "
                       "try again in a minute.",
    "dispatch_unknown": "⚠️ We lost contact with MTGO while setting up the trade, so we "
                        "can't tell whether it started. Check MTGO for a message from the "
                        "library bot before trying again — nothing has been recorded.",
    "nothing_held": "📭 The library isn't holding any of your cards.",
    # start_deposit and start_withdrawal both refuse an order too big for one
    # MTGO trade, and both commands now chunk before they ask, so neither should
    # reach this. Kept as a backstop rather than deleted: a status with no entry
    # reaches the player as its own name. It no longer says splitting is
    # unbuilt -- if this fires, the chunking failed to hold, which is a bug
    # rather than a missing feature.
    "too_large": "📦 That's {detail} — more than a single trade can carry. Nothing "
                 "has moved, and `/library deposits` still shows everything the library "
                 "holds for you. Ask an admin to take a look.",
}


def _custody_said(status: str, fallback: str, detail: Optional[str] = None) -> str:
    """The line for one status. Entries that quote the service's own
    explanation take it as `{detail}` rather than re-deriving it here."""
    return _CUSTODY_MESSAGES.get(status, fallback).format(detail=detail)


_NO_LIBRARY = ("📭 This server isn't set up to borrow from a card library. "
               "Ask whoever runs the library to point it here.")


def describe_deposit_shortfall(figures: "dict[str, int]") -> str:
    """Why the borrow could not be paid for, in the numbers the player needs.

    All of them: the deposit this deck carries, the week's pass where one is
    owed, what their wallet holds, and the difference. Without the gap a player
    cannot tell whether they are one tix short or ten, so the only way forward
    is to top up blind and retry until it works -- which is the same dead end
    /library borrow used to be when the library was short of cards.

    """
    cost = f"a **{figures['deposit']} tix** deposit"
    return (f"💰 Borrowing this deck needs {cost}, and your wallet holds "
            f"**{figures['have']}**. Add **{figures['short']}** more with "
            f"`/wallet` and run the command again — nothing has been charged.")


def library_gate(ctx: Any) -> Optional[str]:
    """Why this guild cannot use the card library, or None.

    Deliberately says nothing about PRICE. What a deck costs is a property of
    the cube it was drafted from, and this runs before any cube is known -- it
    guards /library deck and /library return too, which have no cube until a loan is looked
    up. A guild-wide collateral used to be read here and is not any more: it
    made a free cube demand a wallet, because the number it consulted had
    stopped being the one that decides.

    The wallet requirement moved to where the charge actually happens, which is
    the only place that knows whether this particular cube costs anything.
    """
    if not getattr(ctx, "guild", None):
        return "The card library can only be used in a server."
    # Checked HERE rather than left to the dispatch, which is behind the queue:
    # a disabled client makes vault() answer None (so nothing reads as short)
    # and the busy check read as "can't reach the custodian", so the borrower
    # was told they were next, waited out the five-minute queue and got "try
    # again shortly" -- an invitation to repeat it. Mirrors gate_serve.
    if not get_lending_client().enabled:
        return ("The card library isn't configured on this bot "
                "(set MTGO_LENDING_URL and MTGO_LENDING_TOKEN).")
    return None


def describe_shortfall(short: "list[dict[str, Any]]") -> str:
    """Name what the library is short, and by how much.

    The number they have to find elsewhere is the actionable part -- "not
    enough Swamps" leaves a player guessing whether that means one or nine.
    """
    if not short:
        return ""
    lines = []
    for item in short:
        name, want, have = item["name"], item["want"], item["have"]
        if have <= 0:
            lines.append(f"• **{name}** — none available (you need {want})")
        else:
            lines.append(f"• **{name}** — only **{have}** of {want} available; "
                         f"you'd need to source the other **{want - have}**")
    return _within_a_message(lines)


# Discord refuses a message over 2000 characters, and the send RAISES -- so an
# over-long shortfall does not get truncated, it gets no reply at all. Every
# loan used to be a hand-seeded fixture of two or three cards; a drafted pool is
# up to 48 distinct names, and a library that covers none of them names all 48
# at ~60 characters each. The budget leaves room for the surrounding message.
_SHORTFALL_BUDGET = 1500


def _within_a_message(lines: "list[str]") -> str:
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


def _deck_lines(loan: Any) -> str:
    # The pending offer in preference to the deck, exactly as the dispatch reads
    # it: a borrower who took a partial deck is sent a trade for the partial
    # deck, and listing the whole one beside it invites them to reject a window
    # that is "missing" cards they were told to expect.
    cards = getattr(loan, "pending_cards", None) or getattr(loan, "cards", None)
    if not cards:            # also covers loan being None
        return ""
    return "\n".join(f"• {c['qty']}× {c['name']}" for c in cards)


async def defer_if_usable(ctx: Any) -> bool:
    """Answer the interaction and say whether the library is usable here.

    Every command that TRADES opens this way, in both cogs. Shared so a guild
    that is not set up is told so in one voice -- and so the defer cannot be
    forgotten in one command, which turns a clear refusal into a silent
    "application did not respond".

    /library deposits is the exception and is right to be: it only reads a ledger,
    needs no serve, and says its own thing when the guild has no library.
    """
    await ctx.defer(ephemeral=True)
    blocked = library_gate(ctx)
    if blocked:
        await ctx.followup.send(blocked, ephemeral=True)
        return False
    return True


def list_cube_in_guild(guild_id: Any, cube_id: Any) -> bool:
    """Offer this cube in the server's own cube list. True if it was added.

    Through the bot's config API rather than the file on disk: every config is
    loaded into memory at startup and `save_config` writes that copy back, so
    an edit made to the file underneath a running bot is clobbered by its next
    save.

    A server inheriting the default cube list has no block of its own, and
    gaining one makes its list explicit -- it stops picking up later changes to
    the default. That is a real consequence, and the reason this only happens
    where a server has said it is communal.
    """
    config = get_config(str(guild_id))
    cubes = config.setdefault("cubes", {})
    entries = cubes.setdefault("default", list(
        bot_config.default_config["cubes"]["default"]))
    if any(e.get("value") == str(cube_id) for e in entries):
        return False
    entries.append({"label": str(cube_id), "value": str(cube_id)})
    save_config(str(guild_id), config)
    logger.info("library: {} now offers {} in its cube list", guild_id, cube_id)
    return True


def _count(cards: "list[dict[str, Any]]") -> int:
    """Copies, not distinct names -- four Bolts are four cards to a trade."""
    return sum(int(c["qty"]) for c in cards)


async def cards_to_deposit(cards: "list[dict[str, Any]]", library_id: Any,
                           full_copy: bool = False,
                           copies: int = 1) -> "list[dict[str, Any]]":
    """What to actually offer the serve for this cube.

    By default, what the library is SHORT -- which for an updated cube is
    exactly the cards that were added to it. Topping up is the common case,
    because a cube gets maintained far more often than it gets a second copy,
    and it is the safe default: it can never quietly take a second copy of
    something the library already has.

    That matters because the binder cannot do the filtering. Somebody who owns
    two copies of a cube physically HAS the cards, so offering the whole list
    would hand over a second copy of everything whether they meant to or not.

    Measured against the whole LIBRARY being given to, rather than against
    what this cube put there. One Lightning Bolt serves whichever cube needs a Bolt, so a card
    already held is not asked for again just because a different cube supplied
    it -- the library is an inventory, not a shelf per cube.

    `copies` is how many of this cube the shelf should be able to field. Two
    cubes that share a card cannot both fire on one copy of it, because
    whichever drafts first takes it; asking for two tops the shelf up to a
    count that serves both. It is a TARGET, which is what makes it idempotent:
    several people can deposit toward it, each giving what they own, without
    the target moving and without anybody coordinating who covers what. A
    boolean could not do that -- two donors would double up on some cards and
    leave gaps on others.

    `full_copy` ignores the target and offers the cube as it stands, for
    somebody who just wants to hand one over.
    """
    if full_copy:
        return cards
    from services.card_library_inventory import cube_support, library_holdings

    wanted = ([{"name": c["name"], "qty": int(c["qty"]) * copies} for c in cards]
              if copies != 1 else cards)
    short = cube_support(wanted, await library_holdings(library_id)).missing
    return [{"name": m["name"], "qty": m["short"]} for m in short]


def left_out_note(dropped: "list[str]") -> str:
    """What to tell a depositor about cards that cannot cross.

    Named rather than counted, because this is the only place anybody learns
    these cards are a problem and a name is the only form the cube's owner can
    act on. Empty for the ordinary cube, which is nearly every cube -- a note
    that fires on every deposit stops being read.
    """
    if not dropped:
        return ""
    one = len(dropped) == 1
    said = (f"{card_count_label(len(dropped))} "
            f"{'isn' if one else 'aren'}'t on MTGO, so "
            f"{'it was' if one else 'they were'} left out")
    return f"\n\n⚠️ {said}:\n{_within_a_message([f'> {n}' for n in dropped])}"


def describe_cube(cards: "list[dict[str, Any]]") -> str:
    """A cube in one line. The card list itself belongs in the trade window, not
    in an embed -- three hundred names is not something anyone reads here."""
    total = sum(int(c.get("qty") or 0) for c in cards)
    distinct = len(cards)
    return (f"**{total}** cards" if total == distinct
            else f"**{total}** cards ({distinct} distinct)")


class TakeWhatIsThereView(discord.ui.View):
    """Offer the partial deck when the library cannot cover all of it.

    Deliberately asked BEFORE queueing for the serve: the library trades with
    one person at a time, and holding that place while a human decides would
    block everyone else behind a dialog nobody else can see.
    """

    def __init__(self, cog: Any, ctx: Any, loan_id: Any, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.cog, self.ctx, self.loan_id = cog, ctx, loan_id

    @discord.ui.button(label="Take what's available", style=discord.ButtonStyle.primary)
    async def take(self, button: "discord.ui.Button[Any]",
                   interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        trimmed = await trim_to_available(self.ctx.guild_id, self.loan_id)
        if not trimmed:
            await interaction.followup.send(
                "📭 There's nothing left of that deck to lend right now.", ephemeral=True)
            self.stop()
            return
        lines = "\n".join(f"• {c['qty']}× {c['name']}" for c in trimmed)
        await interaction.followup.send(f"👍 Borrowing what's there:\n{lines}", ephemeral=True)
        self.stop()
        # The agreed subset rides along as an argument. A second click cannot
        # change what the first one is moving, because the offer is only written
        # down as part of the trade that carries it.
        spawn_followup("card-library borrow",
                       self.cog._borrow_when_ready(self.ctx, offering=trimmed))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: "discord.ui.Button[Any]",
                     interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "👌 Left your deck as it is — nothing borrowed.", ephemeral=True)
        self.stop()


class LibraryCommands(commands.Cog):
    """All six library commands, under one `/library` group.

    Grouped rather than left at the top level because these are not six things
    a server does, they are one thing with six verbs -- and as bare top-level
    commands, "borrow", "return" and "deposit" are words a Discord server uses
    for plenty that has nothing to do with this bot. The group also keeps the
    feature together for a server that has no library: six greyed-out commands
    become one.
    """

    library = SlashCommandGroup(
        "library", "Borrow a deck from the card library, or lend it your cards")

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def _warn_if_busy(self, ctx: Any) -> None:
        """The library trades with one person at a time. Say so up front rather
        than opening a trade window that will not appear for minutes, and keep
        their place in line instead of asking them to come back."""
        if await library_busy_reason():
            await ctx.followup.send(
                "🕑 The library is trading with someone else right now — you're next. "
                "I'll message you the moment your trade is ready, so you can stay put.",
                ephemeral=True)

    async def _trade_when_ready(self, ctx: Any,
                                when_free: "Callable[[Any, Any], Awaitable[tuple[str, bool]]]",
                                verb: str, expect: str, note: str = "") -> None:
        """Wait for the library's turn, dispatch, then see the trade through.

        Runs detached so the interaction is not held open for the wait: Discord
        gives a command 15 minutes of followups, but a player staring at a
        spinner for five of them will assume it broke.
        """
        status, waited = await when_free(ctx.guild_id, ctx.author.id)
        if status == "short_funds":
            # The figures are a nicety; answering at all is not. Rendering them
            # costs a wallet read, so unlike the static strings this branch can
            # raise -- and a raise here leaves the interaction deferred and
            # never answered, which Discord shows as "did not respond".
            try:
                figures = await deposit_shortfall(ctx.guild_id, ctx.author.id)
                said = (describe_deposit_shortfall(figures) if figures
                        else _LOAN_MESSAGES[status])
            except Exception:
                logger.exception("library: could not read the deposit figures for {}",
                                 ctx.author.id)
                said = _LOAN_MESSAGES[status]
            await ctx.followup.send(said, ephemeral=True)
            return
        if status != "dispatched":
            await ctx.followup.send(
                _loan_said(status, f"⚠️ Couldn't {verb} your deck ({status})."),
                ephemeral=True)
            return

        loan = await active_loan(ctx.author.id)
        who = await custodian_name(get_lending_client())
        lead = "✅ **Your turn.** " if waited else ""
        job_id = getattr(loan, "job_id", None)
        await ctx.followup.send(
            f"{lead}🤝 **Your deck is ready to {verb}.** {mtgo_trade_prompt(who)}\n"
            # Only when the bot is TAKING cards. Collecting a deck gives cards
            # to the player, who offers nothing and needs no advice about how.
            f"{full_trade_list_advice(exact_printing=True) if expect == 'returned' else ''}\n\n"
            f"{_deck_lines(loan)}"
            f"{mtgo_job_footer(job_id) if job_id else ''}{note}",
            ephemeral=True)

        # Wait on the job so the player gets a real answer rather than silence.
        # The watchdog covers anything that outlives this poll.
        outcome, detail = await poll_until_settled(ctx.guild_id, ctx.author.id, expect,
                                                   job_id=job_id)
        await self._report_outcome(ctx, outcome, expect, detail)

    @library.command(name="borrow",
                      description="Collect the deck the card library is holding for you")
    async def borrow(self, ctx: discord.ApplicationContext) -> None:
        logger.info("/library borrow by {} in guild {}", ctx.author.id, ctx.guild_id)
        if not await defer_if_usable(ctx):
            return

        # Only borrowing is gated. Depositing and withdrawing stay open: somebody
        # contributing cards is not the risk, and a sponsor locked out of their
        # own deposits would be absurd.
        # Asked of the LIBRARY, not the server: somebody trusted with a
        # sponsor's cards is trusted with them in every room it lends into.
        library_id = await library_id_for(ctx.guild_id)
        if library_id is None:
            await ctx.followup.send(
                "📭 This server isn't set up to borrow from a card library.",
                ephemeral=True)
            return
        loan = await active_loan(ctx.author.id)
        if loan is not None:
            library_id = getattr(loan, "library_id", None) or library_id
        if not await may_borrow(library_id, ctx.author.id):
            await ctx.followup.send(
                "🔒 This card library is invite-only at the moment, and you're "
                "not on the list. Ask whoever runs it if you'd like to be.",
                ephemeral=True)
            return

        # Can the library actually cover this deck? Asked before queueing, so a
        # shortfall is reported now rather than surfacing minutes later as a
        # failed MTGO trade the player was told to go and accept.
        if loan is not None and loan.state == "assigned":
            short = await shortfall(ctx.guild_id, loan.cards or [], loan.id)
            if short:
                view = TakeWhatIsThereView(self, ctx, loan.id)
                await ctx.followup.send(
                    f"📦 The library can't cover your whole deck right now:\n"
                    f"{describe_shortfall(short)}\n\n"
                    f"You can take what's there and source the rest yourself — "
                    f"your `/library return` will only ask for what you actually borrowed.",
                    view=view, ephemeral=True)
                return

        await self._warn_if_busy(ctx)
        spawn_followup("card-library borrow", self._borrow_when_ready(ctx))

    def _borrow_when_ready(self, ctx: Any,
                           offering: "Optional[list[dict[str, Any]]]" = None
                           ) -> "Awaitable[None]":
        return self._trade_when_ready(
            ctx, lambda g, b: borrow_when_free(g, b, offering=offering),
            "collect", "borrowed",
            note="\n⚠️ When you're done, **move these cards into your MTGO trade "
                 "binder** before `/library return` — the bot can only take back what's "
                 "in your binder.")

    async def _report_outcome(self, ctx: Any, outcome: str, expected: str,
                              detail: Optional[str] = None) -> None:
        """Say what happened, and for a failure, what to do about it.

        A failed borrow and a failed return need opposite advice: the first
        leaves the deck on the shelf, the second leaves it with the borrower.
        Both carry the serve's own reason, which is usually the only actionable
        part -- a short binder or a mistyped username is the player's to fix.
        """
        if outcome == "borrowed":
            await ctx.followup.send(
                "✅ Deck collected — it's in your MTGO account.\n"
                "_Move it into your **trade binder** before `/library return`, or the bot "
                "can't take it back._", ephemeral=True)
        elif outcome == "returned":
            await ctx.followup.send("✅ Deck returned. Thanks!", ephemeral=True)
        elif outcome == "failed":
            why = explain_trade_failure(detail or "the trade didn't complete")
            if expected == "returned":
                await ctx.followup.send(
                    f"❌ The deck didn't come back, so it's **still with you** and still "
                    f"on loan.\n\n{why}\n\nRun `/library return` again when it's sorted.",
                    ephemeral=True)
            else:
                await ctx.followup.send(
                    f"❌ The trade didn't complete, so no cards moved — your deck is "
                    f"**still reserved** and your deposit is back.\n\n{why}\n\n"
                    f"Run `/library borrow` again when you're ready — it costs no more.",
                    ephemeral=True)
        # still running: the watchdog will settle it; saying nothing is correct

    @library.command(name="return",
                      description="Return the deck you borrowed from the card library")
    async def return_cards(self, ctx: discord.ApplicationContext) -> None:
        logger.info("/library return by {} in guild {}", ctx.author.id, ctx.guild_id)
        if not await defer_if_usable(ctx):
            return
        await self._warn_if_busy(ctx)
        spawn_followup("card-library return", self._trade_when_ready(
            ctx, return_when_free, "return", "returned"))

    # The two "what is the library holding for me" commands answer different
    # questions and sat in different cogs, so both were called that. Side by
    # side under one group they have to say which is which: a deck is out on
    # loan TO you, deposits are cards of yours the library is keeping FOR you.
    @library.command(name="deck",
                      description="The deck the library has for you, and where it is")
    async def deck(self, ctx: discord.ApplicationContext) -> None:
        # Gated like the rest: it reads a loan, and a guild with no library has
        # none to read. Skipping the gate let it answer in a DM, where
        # ctx.guild_id is None.
        if not await defer_if_usable(ctx):
            return
        loan = await active_loan(ctx.author.id)
        if loan is None:
            await ctx.followup.send(_LOAN_MESSAGES["no_loan"], ephemeral=True)
            return
        # Every ACTIVE state, because the fallback renders the raw column value
        # at the player: a loan parked by a dispatch nobody could account for
        # read "Your deck is **dispatch_unknown**".
        where = {
            "assigned": "waiting for you — run `/library borrow`",
            "out_pending": "being traded to you now — accept it in MTGO",
            "borrowed": "with you — `/library return` when you're done",
            "return_pending": "on its way back — accept the trade in MTGO",
            "dispatch_unknown": ("in an unknown state — we lost contact with MTGO "
                                 "while setting up the trade. An admin needs to "
                                 "sort it out; check MTGO for a trade from the "
                                 "library bot in the meantime"),
        }.get(loan.state, loan.state)
        await ctx.followup.send(f"📦 Your deck is **{where}**:\n{_deck_lines(loan)}",
                                ephemeral=True)

    @library.command(name="deposit",
                      description="Lend a cube's cards to the card library")
    @discord.option("cube", str, description="The CubeCobra cube id, e.g. oldmanbudget")
    @discord.option("copies", int, required=False, min_value=1, max_value=8,
                    description="How many drafts of this cube the library should "
                                "be able to field at once (default 1)")
    @discord.option("full_copy", bool, required=False,
                    description="Hand over a whole copy, even cards the library "
                                "already has (default: only what it is missing)")
    async def deposit(self, ctx: discord.ApplicationContext, cube: str,
                      copies: int = 1, full_copy: bool = False) -> None:
        logger.info("/library deposit {} by {} in guild {} (copies={} full_copy={})",
                    cube, ctx.author.id, ctx.guild_id, copies, full_copy)
        if not await defer_if_usable(ctx):
            return
        library = await library_for(ctx.guild_id)
        if library is None:
            await ctx.followup.send(_NO_LIBRARY, ephemeral=True)
            return

        seen = await cube_as_the_library_sees_it(cube)
        cards = seen.cards if seen else None
        untradeable = seen.not_on_mtgo if seen else []
        if cards is None:
            await ctx.followup.send(
                f"🔌 Couldn't read `{cube}` from CubeCobra. Check the cube id "
                f"(it's the bit after `/cube/list/` in the URL) and try again.",
                ephemeral=True)
            return

        offering = await cards_to_deposit(cards, library.id, full_copy, copies)
        if not offering:
            await ctx.followup.send(
                f"✅ The library already has enough for **{copies}** "
                f"{'draft' if copies == 1 else 'simultaneous drafts'} of `{cube}` "
                f"— nothing to hand over. Raise `copies`, or pass "
                f"`full_copy: True`, to give it more anyway."
                f"{left_out_note(untradeable)}", ephemeral=True)
            return

        chunks = chunk_cards(offering, max_cards_per_trade())
        run = ("" if len(chunks) == 1 else
               f" MTGO only moves **{max_cards_per_trade()}** in one trade, so this "
               f"is **{len(chunks)} trades** — accept each one as it comes.")
        what = (f"`{cube}` — {describe_cube(cards)}" if full_copy
                else f"the **{describe_cube(offering)}** `{cube}` is missing")
        await ctx.followup.send(
            f"📦 Depositing {what}.{run}{left_out_note(untradeable)}"
            f"\n_Setting up…_", ephemeral=True)
        spawn_followup("card-library deposit",
                       self._deposit_and_watch(ctx, chunks, cube, library.id))

    async def _adopt_once_given(self, ctx: Any, cube: str) -> None:
        """List and free this cube here, now that its depositor has given to it.

        A communal library lists what it is given: members bring cubes and
        everyone plays them, so making somebody hand-price each one puts the
        operator in the middle of a thing meant to need no operator. Curated
        libraries -- anything lending a sponsor's cards -- never do this.

        Held until cards have actually crossed. It used to run before the
        deposit was even worked out, and topping up is the default -- so a cube
        the shelf already covered had nothing to hand over, and the command
        returned having priced that cube free and listed it having contributed
        nothing. In a communal server anybody can run /library deposit, which made that
        a way to open ANY cube the shelf happens to cover, including one
        stocked from somebody else's cards.
        """
        library = await library_for(ctx.guild_id)
        if not is_communal(library) or library is None:
            return
        if not await offer_cube(library.id, cube, "communal:auto"):
            return
        # The price is committed; the listing is a separate store and can fail
        # on its own. Saying so beats a generic command error over a pricing
        # decision the depositor never saw made.
        try:
            list_cube_in_guild(ctx.guild_id, cube)
        except Exception:
            logger.opt(exception=True).error(
                "library: {} was priced free in {} but could not be added to the "
                "server's cube list", cube, ctx.guild_id)
            await ctx.followup.send(
                f"⚠️ `{cube}` is free to borrow here now, but I couldn't add it to "
                f"this server's cube list — ask an admin to add it.", ephemeral=True)
            return
        await ctx.followup.send(
            f"🆓 `{cube}` is now a free cube here, and anyone can pick it for "
            f"a draft. Borrowing from it costs nothing.", ephemeral=True)

    async def _deposit_and_watch(self, ctx: Any,
                                 chunks: "list[list[dict[str, Any]]]",
                                 cube: str, library_id: Any) -> None:
        """Walk the depositor through one trade per chunk.

        Detached so the interaction is not held open for the wait: Discord gives
        a command 15 minutes of followups, but a player staring at a spinner for
        several of them will assume it broke.

        Each chunk is a COMPLETE order -- its own trade, its own job, settled on
        its own -- so a run that stops halfway leaves the cards that did cross
        recorded and the rest untouched. Stopping is the right response to a
        failure here: the later chunks would only fail the same way, and a
        depositor watching trades fail one after another learns nothing.
        """
        # The first thing this does is talk to the serve, so a stall here looks
        # exactly like a stall in the deposit itself from Discord's side: the
        # command has already said "Setting up..." and nothing further is sent.
        logger.debug("deposit run: asking the serve who the custodian is")
        t0 = time.monotonic()
        who = await custodian_name(get_lending_client())
        logger.debug("deposit run: custodian is {} ({:.1f}s)", who, time.monotonic() - t0)
        # Read once, before anything moves. What lands is measured against it
        # rather than counted up from the chunks -- see _so_far.
        t0 = time.monotonic()
        before = _count(await held_for(ctx.author.id, library_id))
        logger.debug("deposit run: already held = {} ({:.1f}s); {} chunk(s) to send",
                     before, time.monotonic() - t0, len(chunks))

        async def stop(message: str, at: int) -> None:
            """Every early exit says the same two things: why it stopped, and
            what got in before it did. Written once so a fourth exit added
            later cannot quietly forget the second half."""
            await ctx.followup.send(
                f"{message}{await self._so_far(ctx, before, at, chunks, library_id)}",
                ephemeral=True)

        for n, chunk in enumerate(chunks, start=1):
            of = "" if len(chunks) == 1 else f" ({n} of {len(chunks)})"
            logger.info("deposit run: chunk {} of {} -- {} cards",
                        n, len(chunks), _count(chunk))
            status, detail = await start_deposit(ctx.guild_id, ctx.author.id, chunk)
            if status != "dispatched":
                await stop(_custody_said(status, f"⚠️ Couldn't deposit those cards ({status}).",
                                 detail), n)
                return

            await ctx.followup.send(
                f"🤝 **Ready to hand over{of}.** {mtgo_trade_prompt(who)}\n"
                f"{mtgo_job_footer(detail) if detail else ''}\n"
                f"{full_trade_list_advice()}",
                ephemeral=True)

            outcome: "dict[str, Any]" = (
                await poll_deposit(ctx.guild_id, detail) if detail else {})
            match outcome.get("state"):
                case "done":
                    if n == 1:
                        await self._adopt_once_given(ctx, cube)
                    continue
                case "failed":
                    why = explain_trade_failure(
                        outcome.get("detail") or "the trade didn't complete")
                    await stop(f"❌ That trade didn't complete.\n\n{why}", n)
                case _:
                    # Still open, or the poller gave up waiting -- either way the
                    # watchdog settles it. Starting the next trade now would queue
                    # one behind a trade they have not accepted yet.
                    await stop("🕑 That trade is still open in MTGO. I'll record it "
                               "when it lands — check `/library deposits` in a few minutes.", n)
            return

        await ctx.followup.send(
            f"✅ Deposited. The library is now holding "
            f"**{_count(await held_for(ctx.author.id, library_id))}** of your cards "
            f"— `/library deposits` to see them, and they'll come back to you as the same "
            f"printings.", ephemeral=True)

    @staticmethod
    async def _so_far(ctx: Any, before: int, at: int,
                      chunks: "list[list[dict[str, Any]]]",
                      library_id: Any) -> str:
        """What actually crossed before this stopped, read off the ledger.

        Adding up the chunks that were dispatched would report what was ASKED
        for. A depositor whose binder was short sends fewer, and the ledger
        already records the difference -- so the count is taken as the change in
        what the library holds, which is the number /library deposits will agree with.

        It also says to re-run, because that is the recovery and it does not
        look like one: /library deposit asks for what the library is SHORT, measured
        against the ledger these cards have just joined, so the ones that landed
        are not asked for again. (`full_copy` is the exception -- it offers the
        cube as it stands, which is the point of it.) The line used to warn the
        opposite, from before topping up was the default, and deterred the one
        action that finishes the deposit.
        """
        landed = _count(await held_for(ctx.author.id, library_id)) - before
        if landed <= 0:
            return ""
        return (f"\n\n**{landed}** of your cards went in before this "
                f"(trade {at} of {len(chunks)}) — `/library deposits` to see them.\n"
                f"💡 Run `/library deposit` again to send the rest: it asks only for what "
                f"the library is still missing, so those {landed} won't go in twice.")

    @library.command(name="withdraw",
                      description="Take back the cards the card library is holding for you")
    async def withdraw(self, ctx: discord.ApplicationContext) -> None:
        logger.info("/library withdraw by {} in guild {}", ctx.author.id, ctx.guild_id)
        if not await defer_if_usable(ctx):
            return
        library = await library_for(ctx.guild_id)
        if library is None:
            await ctx.followup.send(_NO_LIBRARY, ephemeral=True)
            return
        spawn_followup("card-library withdraw",
                       self._withdraw_and_watch(ctx, library.id))

    async def _withdraw_and_watch(self, ctx: Any, library_id: Any) -> None:
        """Walk the depositor through one trade per order.

        A position bigger than one MTGO trade comes back in pieces, and the
        pieces are run one at a time: opening several trades at once would put
        offers in front of somebody who has not accepted the first.
        """
        orders = await withdrawal_orders(ctx.author.id, library_id)
        if not orders:
            await ctx.followup.send(_custody_said("nothing_held", "📭 Nothing to take back."),
                                    ephemeral=True)
            return

        who = await custodian_name(get_lending_client())
        before = _count(await held_for(ctx.author.id, library_id))

        async def stop(message: str) -> None:
            """Every early exit says what came back before it stopped, read off
            the ledger rather than counted up from the orders."""
            back = before - _count(await held_for(ctx.author.id, library_id))
            tail = ("" if back <= 0 else
                    f"\n\n**{back}** of your cards came back before this — "
                    f"`/library deposits` for the rest.")
            await ctx.followup.send(f"{message}{tail}", ephemeral=True)

        for n, cards in enumerate(orders, start=1):
            of = "" if len(orders) == 1 else f" ({n} of {len(orders)})"
            status, detail = await start_withdrawal(ctx.guild_id, ctx.author.id,
                                                    cards=cards)
            if status == "some_on_loan":
                # Named rather than traded for: the bot's binder is short by
                # exactly what a borrower is holding, so the trade would open
                # and fail.
                await stop(f"📦 Some of your cards are out on loan right now, so "
                           f"the library can't hand them back yet:\n{detail}\n\n"
                           f"Try again once they're returned — `/library deposits` "
                           f"still shows everything it owes you.")
                return
            if status != "dispatched":
                await stop(_custody_said(status, f"⚠️ Couldn't take those back ({status}).",
                                 detail))
                return

            await ctx.followup.send(
                f"🤝 **Ready to hand them back{of}.** {mtgo_trade_prompt(who)}\n"
                f"{mtgo_job_footer(detail) if detail else ''}\n"
                f"_You'll get the same printings you deposited._",
                ephemeral=True)

            outcome: "dict[str, Any]" = (
                await poll_deposit(ctx.guild_id, detail) if detail else {})
            match outcome.get("state"):
                case "done":
                    continue
                case "failed":
                    why = explain_trade_failure(
                        outcome.get("detail") or "the trade didn't complete")
                    await stop(f"❌ That trade didn't complete, so those cards are "
                               f"still with the library.\n\n{why}\n\nRun "
                               f"`/library withdraw` again when it's sorted.")
                case _:
                    await stop("🕑 That trade is still open in MTGO. I'll record it "
                               "when it lands — check `/library deposits` in a few "
                               "minutes, then `/library withdraw` for anything left.")
            return

        left = await held_for(ctx.author.id, library_id)
        tail = ("" if not left else
                f"\nThe library still holds **{_count(left)}** of yours — "
                f"`/library deposits`.")
        await ctx.followup.send(f"✅ Cards returned to your MTGO account.{tail}",
                                ephemeral=True)

    @library.command(name="deposits",
                      description="The cards of yours the library is holding for you")
    async def deposits(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer(ephemeral=True)
        library = await library_for(ctx.guild_id)
        if library is None:
            await ctx.followup.send(_NO_LIBRARY, ephemeral=True)
            return
        held = await held_for(ctx.author.id, library.id)
        if not held:
            await ctx.followup.send(
                "📭 The library isn't holding any of your cards.", ephemeral=True)
            return
        total = sum(c["qty"] for c in held)
        def by_name(card: "dict[str, Any]") -> str:
            return str(card["name"])

        lines = "\n".join(f"• {c['qty']}× {c['name']}"
                          for c in sorted(held, key=by_name)[:40])
        more = "" if len(held) <= 40 else f"\n…and {len(held) - 40} more."
        await ctx.followup.send(
            f"📦 The library is holding **{total}** of your cards:\n{lines}{more}",
            ephemeral=True)


def setup(bot: Any) -> None:
    bot.add_cog(LibraryCommands(bot))
