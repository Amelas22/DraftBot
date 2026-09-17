"""Borrowing a deck from the card library, and giving it back.

Commands:
- /borrow  - collect the deck the library is holding for you
- /return  - hand it back
- /mydeck  - what the library has for you, and where it is

The bot cannot finish an MTGO trade on its own: it offers, and the borrower has
to accept in the client. So every dispatched message here says so. A player who
is not told will run the command, see nothing happen, and the job will sit for
its ten-minute wait and fail -- which looks like a broken bot rather than an
unaccepted trade.
"""
from typing import Any, Awaitable, Callable, Optional

import discord
from discord.ext import commands
from loguru import logger

from config import card_library_collateral, is_money_server
from helpers.money_gate import (
    custodian_name, explain_trade_failure, mtgo_job_footer, mtgo_trade_prompt,
    spawn_followup,
)
from services.mtgo_tradebot_client import get_lending_client, max_cards_per_trade
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

# Every status start_borrow / start_return can return. A status with no entry
# would defer the interaction and never answer it, which Discord shows the
# player as "the application did not respond".
_MESSAGES = {
    "no_loan": "📭 The library has no deck waiting for you.",
    "not_linked": "🔗 Link your MTGO account first with `/link_mtgo <username>`, so the library "
                  "knows who to trade with.",
    "already_in_flight": "⏳ That trade is already open — accept it in MTGO.",
    "already_borrowed": "📦 You already have your deck. Use `/return` when you're done with it.",
    "not_borrowed": "📭 You don't have a deck out from the library.",
    "still_busy": "🕑 The library is still busy after a long wait. Nothing has moved and "
                  "nothing has been charged — try again shortly.",
    # Normally rendered by describe_deposit_shortfall, which has the real
    # figures. This is the fallback for when the wallet cannot be read.
    "short_funds": "💰 You don't have enough tix to cover the deposit on this deck. "
                   "Top up with `/wallet` and try again — nothing has been charged.",
    "unavailable": "🔌 The card library is unavailable right now. Try again shortly.",
    # A callable, not a string: the limit is read from the environment when
    # asked, so baking it into this table at import time would freeze whatever
    # value happened to be loaded first.
    "too_large": lambda: (f"📦 That deck is bigger than MTGO will move in one trade "
                          f"({max_cards_per_trade()} cards). Ask an admin — the "
                          f"library can't hand over a deck this size yet."),
    "dispatch_failed": "⚠️ MTGO didn't accept the trade request. Nothing has been charged — "
                       "try again in a minute.",
    # Deliberately does NOT invite a retry: the request may have reached MTGO
    # and opened a real trade, and a second one would hand out a second deck.
    "dispatch_unknown": "⚠️ We lost contact with MTGO while setting up the trade, so we can't "
                        "tell whether it started. Check MTGO for a message from the library "
                        "bot — if there isn't one, ask an admin to sort out your deposit.",
}


def _said_for(status: str, fallback: str) -> str:
    """The line for one status. Entries may be callables, for the ones whose
    text depends on configuration read at call time rather than at import."""
    text = _MESSAGES.get(status, fallback)
    return text() if callable(text) else text


def describe_deposit_shortfall(figures: "dict[str, int]") -> str:
    """Why the deposit could not be taken, in the numbers the player needs.

    All three of them: the deposit this deck carries, what their wallet holds,
    and the difference. Without the gap a player cannot tell whether they are
    one tix short or ten, so the only way forward is to top up blind and retry
    until it works -- which is the same dead end /borrow used to be when the
    library was short of cards.
    """
    return (f"💰 This deck needs a **{figures['need']} tix** deposit and your wallet "
            f"holds **{figures['have']}**. Add **{figures['short']}** more with "
            f"`/wallet` and run the command again — nothing has been charged.")


def library_gate(ctx: Any) -> Optional[str]:
    """Why this guild cannot use the card library, or None.

    The money-server requirement is conditional on purpose: a library that
    charges no collateral never touches the wallet, and forcing those guilds to
    enable the money stack would gate a card feature behind a tix one. A library
    that DOES charge needs the wallet, and finding that out at transfer time --
    after the borrower has been promised a deck -- is the bad version.
    """
    if not getattr(ctx, "guild", None):
        return "The card library can only be used in a server."
    gid = str(ctx.guild.id)
    # One read: config.library_enabled IS "the collateral reads as a number",
    # so asking both separately re-handled a None this has already excluded.
    collateral = card_library_collateral(gid)
    if collateral is None:
        return "The card library isn't set up on this server."
    # Only a POSITIVE deposit needs the wallet; a library that charges 0 is
    # deliberately free and should not drag the money stack in behind it.
    if collateral > 0 and not is_money_server(gid):
        return ("This server's library asks for a tix deposit, but the wallet "
                "isn't enabled here. Ask an admin to sort one or the other.")
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
    return "\n".join(lines)


def _deck_lines(loan: Any) -> str:
    # The pending offer in preference to the deck, exactly as the dispatch reads
    # it: a borrower who took a partial deck is sent a trade for the partial
    # deck, and listing the whole one beside it invites them to reject a window
    # that is "missing" cards they were told to expect.
    cards = getattr(loan, "pending_cards", None) or getattr(loan, "cards", None)
    if not cards:            # also covers loan being None
        return ""
    return "\n".join(f"• {c['qty']}× {c['name']}" for c in cards)


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


class CardLendingCommands(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def _usable(self, ctx: Any) -> bool:
        """Answer the interaction and say whether the library is usable here."""
        await ctx.defer(ephemeral=True)
        blocked = library_gate(ctx)
        if blocked:
            await ctx.followup.send(blocked, ephemeral=True)
            return False
        return True

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
                said = describe_deposit_shortfall(
                    await deposit_shortfall(ctx.guild_id, ctx.author.id))
            except Exception:
                logger.exception("library: could not read the deposit figures for {}",
                                 ctx.author.id)
                said = _MESSAGES["short_funds"]
            await ctx.followup.send(said, ephemeral=True)
            return
        if status != "dispatched":
            await ctx.followup.send(
                _said_for(status, f"⚠️ Couldn't {verb} your deck ({status})."),
                ephemeral=True)
            return

        loan = await active_loan(ctx.guild_id, ctx.author.id)
        who = await custodian_name(get_lending_client())
        lead = "✅ **Your turn.** " if waited else ""
        job_id = getattr(loan, "job_id", None)
        await ctx.followup.send(
            f"{lead}🤝 **Your deck is ready to {verb}.** {mtgo_trade_prompt(who)}\n\n"
            f"{_deck_lines(loan)}"
            f"{mtgo_job_footer(job_id) if job_id else ''}{note}",
            ephemeral=True)

        # Wait on the job so the player gets a real answer rather than silence.
        # The watchdog covers anything that outlives this poll.
        outcome, detail = await poll_until_settled(ctx.guild_id, ctx.author.id, expect,
                                                   job_id=job_id)
        await self._report_outcome(ctx, outcome, expect, detail)

    @discord.slash_command(name="borrow",
                           description="Collect the deck the card library is holding for you")
    async def borrow(self, ctx: discord.ApplicationContext) -> None:
        logger.info("/borrow by {} in guild {}", ctx.author.id, ctx.guild_id)
        if not await self._usable(ctx):
            return

        # Can the library actually cover this deck? Asked before queueing, so a
        # shortfall is reported now rather than surfacing minutes later as a
        # failed MTGO trade the player was told to go and accept.
        loan = await active_loan(ctx.guild_id, ctx.author.id)
        if loan is not None and loan.state == "assigned":
            short = await shortfall(ctx.guild_id, loan.cards or [])
            if short:
                view = TakeWhatIsThereView(self, ctx, loan.id)
                await ctx.followup.send(
                    f"📦 The library can't cover your whole deck right now:\n"
                    f"{describe_shortfall(short)}\n\n"
                    f"You can take what's there and source the rest yourself — "
                    f"your `/return` will only ask for what you actually borrowed.",
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
                 "binder** before `/return` — the bot can only take back what's "
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
                "_Move it into your **trade binder** before `/return`, or the bot "
                "can't take it back._", ephemeral=True)
        elif outcome == "returned":
            await ctx.followup.send("✅ Deck returned. Thanks!", ephemeral=True)
        elif outcome == "failed":
            why = explain_trade_failure(detail or "the trade didn't complete")
            if expected == "returned":
                await ctx.followup.send(
                    f"❌ The deck didn't come back, so it's **still with you** and still "
                    f"on loan.\n\n{why}\n\nRun `/return` again when it's sorted.",
                    ephemeral=True)
            else:
                await ctx.followup.send(
                    f"❌ The trade didn't complete, so nothing moved — your deck is "
                    f"**still reserved** and any deposit is back.\n\n{why}\n\n"
                    f"Run `/borrow` again when you're ready.", ephemeral=True)
        # still running: the watchdog will settle it; saying nothing is correct

    @discord.slash_command(name="return",
                           description="Return the deck you borrowed from the card library")
    async def return_cards(self, ctx: discord.ApplicationContext) -> None:
        logger.info("/return by {} in guild {}", ctx.author.id, ctx.guild_id)
        if not await self._usable(ctx):
            return
        await self._warn_if_busy(ctx)
        spawn_followup("card-library return", self._trade_when_ready(
            ctx, return_when_free, "return", "returned"))

    @discord.slash_command(name="mydeck",
                           description="What the card library is holding for you")
    async def mydeck(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer(ephemeral=True)
        loan = await active_loan(ctx.guild_id, ctx.author.id)
        if loan is None:
            await ctx.followup.send(_MESSAGES["no_loan"], ephemeral=True)
            return
        where = {
            "assigned": "waiting for you — run `/borrow`",
            "out_pending": "being traded to you now — accept it in MTGO",
            "borrowed": "with you — `/return` when you're done",
            "return_pending": "on its way back — accept the trade in MTGO",
        }.get(loan.state, loan.state)
        await ctx.followup.send(f"📦 Your deck is **{where}**:\n{_deck_lines(loan)}",
                                ephemeral=True)


def setup(bot: Any) -> None:
    bot.add_cog(CardLendingCommands(bot))
