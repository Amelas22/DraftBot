"""Lending cards TO the library, and seeing what it holds for you.

- /deposit <cube>  - hand a cube's cards to the library for others to borrow
- /mydeposits      - what the library is holding of yours

The mirror of cogs/card_lending_commands.py, and it works the same way: the bot
offers a trade and the depositor accepts it in MTGO, so every dispatched message
says so. A player who is not told will run the command, see nothing happen, and
the offer will sit for its wait and expire -- which reads as a broken bot rather
than an unaccepted trade.
"""
import time
from typing import Any, Optional

import discord
from discord.ext import commands
from loguru import logger

from services.card_library_inventory import cube_as_the_library_sees_it
from helpers.money_gate import (
    custodian_name, explain_trade_failure, full_trade_list_advice,
    mtgo_job_footer, mtgo_trade_prompt,
    spawn_followup,
)
from services.card_deposit_service import (
    chunk_cards, held_for, poll_until_settled, start_deposit, start_withdrawal,
    withdrawal_orders,
)
from services.mtgo_tradebot_client import get_lending_client, max_cards_per_trade

from config import bot_config, get_config, save_config
from services.library_service import is_communal, library_for, offer_cube

from cogs.card_lending_commands import _within_a_message, defer_if_usable
from debt_views.helpers import card_count_label

_MESSAGES = {
    "nothing_to_deposit": "📭 That cube came back empty — nothing to deposit.",
    "not_linked": "🔗 Link your MTGO account first with `/link_mtgo <username>`, so the "
                  "library knows who to trade with.",
    "unavailable": "🔌 The card library is unavailable right now. Try again shortly.",
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
                 "has moved, and `/mydeposits` still shows everything the library "
                 "holds for you. Ask an admin to take a look.",
}


def _said(status: str, fallback: str, detail: Optional[str] = None) -> str:
    """The line for one status. Entries that quote the service's own
    explanation take it as `{detail}` rather than re-deriving it here."""
    return _MESSAGES.get(status, fallback).format(detail=detail)


_NO_LIBRARY = ("📭 This server isn't set up to borrow from a card library. "
               "Ask whoever runs the library to point it here.")


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


class CardDepositCommands(commands.Cog):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @discord.slash_command(name="deposit",
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
        logger.info("/deposit {} by {} in guild {} (copies={} full_copy={})",
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
        nothing. In a communal server anybody can run /deposit, which made that
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
                await stop(_said(status, f"⚠️ Couldn't deposit those cards ({status}).",
                                 detail), n)
                return

            await ctx.followup.send(
                f"🤝 **Ready to hand over{of}.** {mtgo_trade_prompt(who)}\n"
                f"{mtgo_job_footer(detail) if detail else ''}\n"
                f"{full_trade_list_advice()}",
                ephemeral=True)

            outcome: "dict[str, Any]" = (
                await poll_until_settled(ctx.guild_id, detail) if detail else {})
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
                               "when it lands — check `/mydeposits` in a few minutes.", n)
            return

        await ctx.followup.send(
            f"✅ Deposited. The library is now holding "
            f"**{_count(await held_for(ctx.author.id, library_id))}** of your cards "
            f"— `/mydeposits` to see them, and they'll come back to you as the same "
            f"printings.", ephemeral=True)

    @staticmethod
    async def _so_far(ctx: Any, before: int, at: int,
                      chunks: "list[list[dict[str, Any]]]",
                      library_id: Any) -> str:
        """What actually crossed before this stopped, read off the ledger.

        Adding up the chunks that were dispatched would report what was ASKED
        for. A depositor whose binder was short sends fewer, and the ledger
        already records the difference -- so the count is taken as the change in
        what the library holds, which is the number /mydeposits will agree with.

        It also says to re-run, because that is the recovery and it does not
        look like one: /deposit asks for what the library is SHORT, measured
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
                f"(trade {at} of {len(chunks)}) — `/mydeposits` to see them.\n"
                f"💡 Run `/deposit` again to send the rest: it asks only for what "
                f"the library is still missing, so those {landed} won't go in twice.")

    @discord.slash_command(name="withdraw",
                           description="Take back the cards the card library is holding for you")
    async def withdraw(self, ctx: discord.ApplicationContext) -> None:
        logger.info("/withdraw by {} in guild {}", ctx.author.id, ctx.guild_id)
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
            await ctx.followup.send(_said("nothing_held", "📭 Nothing to take back."),
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
                    f"`/mydeposits` for the rest.")
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
                           f"Try again once they're returned — `/mydeposits` "
                           f"still shows everything it owes you.")
                return
            if status != "dispatched":
                await stop(_said(status, f"⚠️ Couldn't take those back ({status}).",
                                 detail))
                return

            await ctx.followup.send(
                f"🤝 **Ready to hand them back{of}.** {mtgo_trade_prompt(who)}\n"
                f"{mtgo_job_footer(detail) if detail else ''}\n"
                f"_You'll get the same printings you deposited._",
                ephemeral=True)

            outcome: "dict[str, Any]" = (
                await poll_until_settled(ctx.guild_id, detail) if detail else {})
            match outcome.get("state"):
                case "done":
                    continue
                case "failed":
                    why = explain_trade_failure(
                        outcome.get("detail") or "the trade didn't complete")
                    await stop(f"❌ That trade didn't complete, so those cards are "
                               f"still with the library.\n\n{why}\n\nRun "
                               f"`/withdraw` again when it's sorted.")
                case _:
                    await stop("🕑 That trade is still open in MTGO. I'll record it "
                               "when it lands — check `/mydeposits` in a few "
                               "minutes, then `/withdraw` for anything left.")
            return

        left = await held_for(ctx.author.id, library_id)
        tail = ("" if not left else
                f"\nThe library still holds **{_count(left)}** of yours — "
                f"`/mydeposits`.")
        await ctx.followup.send(f"✅ Cards returned to your MTGO account.{tail}",
                                ephemeral=True)

    @discord.slash_command(name="mydeposits",
                           description="What the card library is holding for you")
    async def mydeposits(self, ctx: discord.ApplicationContext) -> None:
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
    bot.add_cog(CardDepositCommands(bot))
