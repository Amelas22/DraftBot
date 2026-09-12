"""House card lending: the vault hands real cards to a player and takes those copies back.

Distinct from `/lend`, which records a loan BETWEEN TWO PLAYERS and moves nothing. Here the
house is the lender and an actual MTGO trade happens, so the surfaces differ:

  * `/cards lend` is BOT-MANAGER ONLY. It gives assets out of the vault, and nothing in the
    system caps who may borrow what or how much. Until a lending policy exists, an open
    self-serve borrow would be a giveaway. Deliberately the safe default; relaxing it later
    is a one-line change, whereas reclaiming cards handed to everybody is not.
  * `/cards return` and `/cards mine` are open to anyone, because returning assets and
    reading your own position cannot cost the house anything.

No printing appears in this file. A player asks for "Lightning Bolt" and gets copies back by
name; the serve remembers which printings crossed and pins them itself on the way home.
"""
import discord
from discord.commands import SlashCommandGroup, option
from discord.ext import commands
from loguru import logger

from services import mtgo_resolution_service as resolution
from services.mtgo_tradebot_client import get_client
from helpers.money_gate import (
    DEFAULT_WAIT_MINUTES, custodian_name, explain_trade_failure, gate_read, gate_serve,
    linked_username, mtgo_job_footer, mtgo_trade_prompt, spawn_followup,
)
from helpers.permissions import has_bot_manager_role


def _copies(card: str, qty: int) -> str:
    return f"{qty}x {card}" if qty != 1 else card


class CardsCog(commands.Cog):
    """Borrowing real cards from the house vault."""

    def __init__(self, bot):
        self.bot = bot

    cards = SlashCommandGroup("cards", "Borrow cards from the vault and return them")

    # ----- /cards lend @player <card> <qty> -----
    @cards.command(name="lend",
                   description="[Manager] Lend cards from the vault to a player")
    @has_bot_manager_role()
    @option("player", discord.Member, description="Who is borrowing")
    @option("card", str, description="Card name, exactly as MTGO spells it")
    @option("quantity", int, description="How many copies", default=1, min_value=1)
    async def cards_lend(self, ctx: discord.ApplicationContext, player: discord.Member,
                         card: str, quantity: int = 1):
        await ctx.defer(ephemeral=True)
        err = gate_serve(ctx)
        if err:
            return await ctx.followup.send(err, ephemeral=True)

        username = await linked_username(player.id)
        if not username:
            return await ctx.followup.send(
                f"{player.display_name} has not linked an MTGO account yet "
                f"(`/link_mtgo <username>`).", ephemeral=True)

        guild_id, player_id = str(ctx.guild.id), str(player.id)
        started = await resolution.start_borrow(
            guild_id, player_id, username, card, quantity,
            commit=True, wait_minutes=DEFAULT_WAIT_MINUTES)
        if not started.get("ok"):
            prefix = "⏳" if started.get("busy") else "Couldn't start the loan:"
            return await ctx.followup.send(f"{prefix} {started.get('error')}", ephemeral=True)

        job_id = started["job_id"]
        custodian = await custodian_name()
        label = _copies(card, quantity)
        await ctx.followup.send(
            f"**Loan started** — sending **{label}** to {player.display_name} ({username}). "
            f"They need to accept the trade. {mtgo_trade_prompt(custodian)}"
            f"{mtgo_job_footer(job_id)}", ephemeral=True)

        followup = ctx.followup

        async def _finish():
            # The obligation is booked inside finish_borrow, and ONLY if the trade
            # completed — a declined trade leaves the player owing nothing.
            res = await resolution.finish_borrow(job_id, guild_id, player_id, card, quantity)
            if res.get("ok"):
                msg = (f"✅ Loan delivered: **{label}** to {player.display_name}. "
                       f"They owe the vault those copies back — `/cards return` settles it.")
            elif res.get("outcome") == "pending":
                msg = (f"⏳ Loan `{job_id}` is still running. Nothing is owed until it "
                       f"completes; it will be booked automatically when it does.")
            else:
                msg = (f"❌ Loan `{job_id}` failed: {explain_trade_failure(res.get('error'))}\n"
                       f"No cards moved and nothing is owed.")
            await followup.send(msg, ephemeral=True)

        spawn_followup("cards lend", _finish())

    # ----- /cards return <card> [qty] -----
    @cards.command(name="return",
                   description="Return cards you borrowed from the vault")
    @option("card", str, description="Card name to hand back")
    @option("quantity", int, description="How many copies (default: all of them)",
            default=None, min_value=1, required=False)
    async def cards_return(self, ctx: discord.ApplicationContext, card: str,
                           quantity: int = None):
        await ctx.defer(ephemeral=True)
        err = gate_serve(ctx)
        if err:
            return await ctx.followup.send(err, ephemeral=True)
        username = await linked_username(ctx.author.id)
        if not username:
            return await ctx.followup.send(
                "Link your MTGO account first with `/link_mtgo <username>`.", ephemeral=True)

        guild_id, player_id = str(ctx.guild.id), str(ctx.author.id)
        started = await resolution.start_return(
            guild_id, player_id, username, card, quantity,
            commit=True, wait_minutes=DEFAULT_WAIT_MINUTES)
        if not started.get("ok"):
            prefix = "⏳" if started.get("busy") else "Couldn't start the return:"
            return await ctx.followup.send(f"{prefix} {started.get('error')}", ephemeral=True)

        job_id, qty = started["job_id"], started["quantity"]
        custodian = await custodian_name()
        label = _copies(card, qty)
        await ctx.followup.send(
            f"**Return started** — hand back **{label}**. The vault asks for the exact "
            f"copies it lent you. {mtgo_trade_prompt(custodian)}"
            f"{mtgo_job_footer(job_id)}", ephemeral=True)

        followup = ctx.followup

        async def _finish():
            res = await resolution.finish_return(job_id, guild_id, player_id, card, qty)
            if res.get("ok"):
                msg = f"✅ Returned **{label}**. That loan is settled."
            elif res.get("outcome") == "pending":
                msg = (f"⏳ Return `{job_id}` is still running; the loan stays open until "
                       f"it completes.")
            else:
                msg = (f"❌ Return `{job_id}` failed: {explain_trade_failure(res.get('error'))}\n"
                       f"You still have those copies and still owe them back.")
            await followup.send(msg, ephemeral=True)

        spawn_followup("cards return", _finish())

    # ----- /cards mine -----
    @cards.command(name="mine", description="Cards you have borrowed from the vault")
    async def cards_mine(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)
        err = gate_read(ctx)
        if err:
            return await ctx.followup.send(err, ephemeral=True)
        username = await linked_username(ctx.author.id)
        if not username:
            return await ctx.followup.send(
                "Link your MTGO account first with `/link_mtgo <username>`.", ephemeral=True)

        # Read the SERVE, not the debt ledger: it watched the cards cross and is the only
        # record of which printings they were.
        try:
            pos = await get_client().positions(username)
        except Exception as e:
            logger.warning(f"/cards mine: positions read failed for {username}: {e}")
            pos = None
        if pos is None:
            return await ctx.followup.send(
                "Couldn't reach the custodian to check. Try again shortly.", ephemeral=True)

        lent = pos.get("lent") or []
        held = pos.get("held") or []
        if not lent and not held:
            return await ctx.followup.send(
                "You have nothing out with the vault.", ephemeral=True)

        embed = discord.Embed(title="Your cards with the vault", color=discord.Color.blue())
        if lent:
            # Printings are shown because a borrower may hold two of the same card from
            # different sets, and the return will ask for those specific ones.
            embed.add_field(
                name="Borrowed from the vault (you owe these back)",
                value="\n".join(f"**{p.get('qty')}x** {p.get('card')} "
                                f"*(printing {p.get('catId')})*" for p in lent),
                inline=False)
        if held:
            embed.add_field(
                name="Deposited with the vault (yours, held for you)",
                value="\n".join(f"**{p.get('qty')}x** {p.get('card')} "
                                f"*(printing {p.get('catId')})*" for p in held),
                inline=False)
        embed.set_footer(text="/cards return <card> hands borrowed copies back")
        await ctx.followup.send(embed=embed, ephemeral=True)


def setup(bot):
    bot.add_cog(CardsCog(bot))
