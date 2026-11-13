"""Player commands for escrow: lock tix for a named counterparty, and see your escrows.

Only a moderator can release or return the tix once they are locked; the commands here
just open the hold and report on it. Every reply is ephemeral and sent with mentions
suppressed, so the parties render as names without being pinged.
"""
import discord
from discord.commands import SlashCommandGroup, option
from discord.ext import commands

from escrow_admin_view import EscrowPanel
from helpers.escrow_text import NO_MENTIONS, describe_escrow
from helpers.money_gate import gate_read, short_funds_message, unlinked_refusal
from helpers.permissions import has_bot_manager_role
from helpers.utils import not_none
from services import escrow_service, wallet_service
from services.wallet_service import InsufficientFunds

DISCORD_MESSAGE_LIMIT = 2000

# Enough for "\n…and 9999 more." with room to spare.
_MORE_LINE_RESERVE = 30

def _fit_to_message(lines: list[str]) -> str:
    """Join lines, cutting from the end to stay under Discord's limit.

    The service returns open escrows first and they are unbounded, so a long list ends
    with how many were left out rather than failing to send.
    """
    whole = "\n".join(lines)
    if len(whole) <= DISCORD_MESSAGE_LIMIT:
        return whole
    kept: list[str] = []
    used = 0
    for line in lines:
        used += len(line) + 1
        if used > DISCORD_MESSAGE_LIMIT - _MORE_LINE_RESERVE:
            break
        kept.append(line)
    return "\n".join(kept + [f"…and {len(lines) - len(kept)} more."])


async def _reply(ctx: discord.ApplicationContext, message: str) -> None:
    await ctx.followup.send(message, ephemeral=True, allowed_mentions=NO_MENTIONS)


class EscrowCommands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    escrow = SlashCommandGroup("escrow", "Lock tix for another player until a moderator decides")

    @escrow.command(name="open", description="Lock tix for another player until a moderator decides")
    @option("player", discord.Member, description="Who the tix are for")
    @option("amount", int, description="How many tix to lock", min_value=1)
    @option("note", str, description="What the escrow is for", required=True,
            max_length=escrow_service.NOTE_MAX)
    async def escrow_open(self, ctx: discord.ApplicationContext, player: discord.Member,
                          amount: int, note: str) -> None:
        await ctx.defer(ephemeral=True)

        err = gate_read(ctx)
        if err:
            return await _reply(ctx, err)
        author = not_none(ctx.author)
        if player.id == author.id:
            return await _reply(ctx, "You can't open an escrow with yourself.")
        if player.bot:
            return await _reply(ctx, "You can't open an escrow with a bot.")
        refusal = await unlinked_refusal(author, player)
        if refusal:
            return await _reply(ctx, refusal)

        guild_id = str(not_none(ctx.guild).id)
        try:
            hold = await escrow_service.open_escrow(
                guild_id, str(author.id), str(player.id), amount, note)
        except InsufficientFunds as e:
            return await _reply(ctx, short_funds_message(e.available, amount, "holding"))
        except ValueError as e:
            # Discord's `required` lets a whitespace-only note through; the service is
            # the one that knows what a usable note is.
            return await _reply(ctx, str(e))

        balance = await wallet_service.get_balance(guild_id, str(author.id))
        await _reply(ctx, 
            f"Escrow **#{hold.id}** opened: **{amount} tix** held for <@{player.id}>. "
            f"Only a moderator can release or return them. Your balance: **{balance} tix**.")

    @escrow.command(name="mine", description="Your escrows, open and recently decided")
    async def escrow_mine(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer(ephemeral=True)
        err = gate_read(ctx)
        if err:
            return await _reply(ctx, err)
        holds = await escrow_service.escrows_for(
            str(not_none(ctx.guild).id), str(not_none(ctx.author).id))
        text = (_fit_to_message([describe_escrow(h) for h in holds]) if holds
                else "You have no escrows on this server.")
        await _reply(ctx, text)

    escrow_admin = SlashCommandGroup("escrow-admin", "Admin commands for escrow")

    @escrow_admin.command(name="list", description="[Admin] Release or return open escrows")
    @has_bot_manager_role()
    async def escrow_admin_list(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer(ephemeral=True)
        err = gate_read(ctx)
        if err:
            return await _reply(ctx, err)
        guild = not_none(ctx.guild)
        panel = await EscrowPanel.build(str(guild.id), guild=guild)
        panel.interaction = ctx.interaction
        await ctx.followup.send(
            embed=panel.embed, view=panel, ephemeral=True, allowed_mentions=NO_MENTIONS)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(EscrowCommands(bot))
