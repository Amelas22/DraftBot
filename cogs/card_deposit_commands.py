"""Lending cards TO the library, and seeing what it holds for you.

- /deposit <cube>  - hand a cube's cards to the library for others to borrow
- /mydeposits      - what the library is holding of yours

The mirror of cogs/card_lending_commands.py, and it works the same way: the bot
offers a trade and the depositor accepts it in MTGO, so every dispatched message
says so. A player who is not told will run the command, see nothing happen, and
the offer will sit for its wait and expire -- which reads as a broken bot rather
than an unaccepted trade.
"""
from typing import Any, Optional

import discord
from discord.ext import commands
from loguru import logger

from helpers.cube_list import fetch_cube
from helpers.money_gate import (
    custodian_name, explain_trade_failure, mtgo_job_footer, mtgo_trade_prompt,
    spawn_followup,
)
from services.card_deposit_service import (
    chunk_cards, held_for, poll_until_settled, start_deposit, start_withdrawal,
)
from services.mtgo_tradebot_client import get_lending_client, max_cards_per_trade

from cogs.card_lending_commands import library_gate

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
}


def _said(status: str, fallback: str) -> str:
    """The line for one status, resolving the entries that depend on
    configuration read at call time rather than at import."""
    text = _MESSAGES.get(status, fallback)
    return str(text() if callable(text) else text)


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
    async def deposit(self, ctx: discord.ApplicationContext, cube: str) -> None:
        logger.info("/deposit {} by {} in guild {}", cube, ctx.author.id, ctx.guild_id)
        await ctx.defer(ephemeral=True)
        blocked = library_gate(ctx)
        if blocked:
            await ctx.followup.send(blocked, ephemeral=True)
            return

        cards = await fetch_cube(cube)
        if cards is None:
            await ctx.followup.send(
                f"🔌 Couldn't read `{cube}` from CubeCobra. Check the cube id "
                f"(it's the bit after `/cube/list/` in the URL) and try again.",
                ephemeral=True)
            return

        chunks = chunk_cards(cards, max_cards_per_trade())
        run = ("" if len(chunks) == 1 else
               f" MTGO only moves **{max_cards_per_trade()}** in one trade, so this "
               f"is **{len(chunks)} trades** — accept each one as it comes.")
        await ctx.followup.send(
            f"📦 Depositing `{cube}` — {describe_cube(cards)}.{run}\n"
            f"_Setting up…_", ephemeral=True)
        spawn_followup("card-library deposit", self._deposit_and_watch(ctx, chunks))

    async def _deposit_and_watch(self, ctx: Any,
                                 chunks: "list[list[dict[str, Any]]]") -> None:
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
        who = await custodian_name(get_lending_client())
        landed = 0
        for n, chunk in enumerate(chunks, start=1):
            of = "" if len(chunks) == 1 else f" ({n} of {len(chunks)})"
            status, detail = await start_deposit(ctx.guild_id, ctx.author.id, chunk)
            if status != "dispatched":
                fallback = f"⚠️ Couldn't deposit those cards ({status})."
                await ctx.followup.send(
                    f"{_said(status, fallback)}"
                    f"{self._so_far(landed, n, chunks)}", ephemeral=True)
                return

            await ctx.followup.send(
                f"🤝 **Ready to hand over{of}.** {mtgo_trade_prompt(who)}\n"
                f"{mtgo_job_footer(detail) if detail else ''}\n"
                f"⚠️ Move the cards into your **MTGO trade binder** first — the bot "
                f"can only take what's in your binder.",
                ephemeral=True)

            outcome: "dict[str, Any]" = (
                await poll_until_settled(ctx.guild_id, detail) if detail else {})
            if outcome.get("state") == "done":
                landed += sum(int(c["qty"]) for c in chunk)
                continue
            if outcome.get("state") == "failed":
                why = explain_trade_failure(
                    outcome.get("detail") or "the trade didn't complete")
                await ctx.followup.send(
                    f"❌ That trade didn't complete.\n\n{why}"
                    f"{self._so_far(landed, n, chunks)}", ephemeral=True)
                return
            # Still running: the watchdog settles it. Starting the next trade now
            # would queue a second one behind a trade they have not accepted yet.
            await ctx.followup.send(
                f"🕑 That trade is still open in MTGO. I'll record it when it "
                f"lands — run `/deposit` again afterwards for the rest."
                f"{self._so_far(landed, n, chunks)}", ephemeral=True)
            return

        held = await held_for(ctx.guild_id, ctx.author.id)
        await ctx.followup.send(
            f"✅ Deposited. The library is now holding **{sum(c['qty'] for c in held)}** "
            f"of your cards — `/mydeposits` to see them, and they'll come back to you "
            f"as the same printings.", ephemeral=True)

    @staticmethod
    def _so_far(landed: int, at: int, chunks: "list[list[dict[str, Any]]]") -> str:
        """What actually crossed before this stopped. A run of several trades
        that ends early has moved real cards, and saying nothing about them
        reads as though the whole thing failed."""
        if len(chunks) == 1 or not landed:
            return ""
        return (f"\n\n**{landed}** cards went in before this "
                f"(trade {at} of {len(chunks)}) — `/mydeposits` to see them.")

    @discord.slash_command(name="withdraw",
                           description="Take back the cards the card library is holding for you")
    async def withdraw(self, ctx: discord.ApplicationContext) -> None:
        logger.info("/withdraw by {} in guild {}", ctx.author.id, ctx.guild_id)
        await ctx.defer(ephemeral=True)
        blocked = library_gate(ctx)
        if blocked:
            await ctx.followup.send(blocked, ephemeral=True)
            return
        spawn_followup("card-library withdraw", self._withdraw_and_watch(ctx))

    async def _withdraw_and_watch(self, ctx: Any) -> None:
        status, detail = await start_withdrawal(ctx.guild_id, ctx.author.id)
        if status == "some_on_loan":
            # Named rather than traded for: the bot's binder is short by exactly
            # what a borrower is holding, so the trade would open and fail.
            await ctx.followup.send(
                f"📦 Some of your cards are out on loan right now, so the library "
                f"can't hand them back yet:\n{detail}\n\nTry again once they're "
                f"returned — `/mydeposits` still shows everything it owes you.",
                ephemeral=True)
            return
        if status == "too_large":
            await ctx.followup.send(
                f"📦 That's {detail}. Taking back this many at once isn't supported yet.",
                ephemeral=True)
            return
        if status != "dispatched":
            await ctx.followup.send(
                _MESSAGES.get(status, f"⚠️ Couldn't take those back ({status})."),
                ephemeral=True)
            return

        who = await custodian_name(get_lending_client())
        await ctx.followup.send(
            f"🤝 **Ready to hand them back.** {mtgo_trade_prompt(who)}\n"
            f"{mtgo_job_footer(detail) if detail else ''}\n"
            f"_You'll get the same printings you deposited._",
            ephemeral=True)

        outcome: "dict[str, Any]" = (
            await poll_until_settled(ctx.guild_id, detail) if detail else {})
        if outcome.get("state") == "done":
            left = await held_for(ctx.guild_id, ctx.author.id)
            tail = ("" if not left else
                    f"\nThe library still holds **{sum(c['qty'] for c in left)}** "
                    f"of yours — `/mydeposits`.")
            await ctx.followup.send(f"✅ Cards returned to your MTGO account.{tail}",
                                    ephemeral=True)
        elif outcome.get("state") == "failed":
            why = explain_trade_failure(outcome.get("detail") or "the trade didn't complete")
            await ctx.followup.send(
                f"❌ The trade didn't complete, so nothing moved and the library still "
                f"holds your cards.\n\n{why}\n\nRun `/withdraw` again when it's sorted.",
                ephemeral=True)

    @discord.slash_command(name="mydeposits",
                           description="What the card library is holding for you")
    async def mydeposits(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer(ephemeral=True)
        held = await held_for(ctx.guild_id, ctx.author.id)
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
