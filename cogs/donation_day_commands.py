"""Donation day commands.

/donation_day paid_cash -- a loser marks their donation debt to DraftBot as
paid in cash (GitHub Sponsors or otherwise). On trust; every mark is logged.
"""
import discord
from discord.commands import SlashCommandGroup
from discord.ext import commands

from helpers.utils import not_none
from services.donation_service import mark_paid_cash


class DonationDayCommands(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    donation_day = SlashCommandGroup("donation_day", "Donation day")

    @donation_day.command(name="paid_cash",
                          description="Mark your donation-draft debt to DraftBot as paid in cash")
    async def paid_cash(self, ctx: discord.ApplicationContext) -> None:
        await ctx.defer(ephemeral=True)
        amount = await mark_paid_cash(str(not_none(ctx.guild).id), str(ctx.author.id),
                                      str(not_none(self.bot.user).id))
        if amount:
            await ctx.followup.send(
                f"Thank you! {amount} tix of donation debt to DraftBot is marked "
                f"paid in cash.", ephemeral=True)
        else:
            await ctx.followup.send(
                "You have no donation debt to DraftBot outstanding.", ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(DonationDayCommands(bot))
