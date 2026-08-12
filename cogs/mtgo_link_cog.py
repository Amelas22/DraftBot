"""
Link a Discord user to their MTGO username.

This is the identity bridge for auto-reporting spectated MTGO match results back to
DraftBot: a completed MTGO game only exposes MTGO usernames, so we must be able to
resolve those to Discord ids before a result can be attributed to the right players.

Commands:
- /link_mtgo <username>            link (or update) your own MTGO account
- /mtgo_whoami                     show your linked MTGO account
- /unlink_mtgo                     remove your link
- /link_mtgo_for @user <username>  (admin) link on a player's behalf — for backfill
"""
import discord
from discord.ext import commands
from loguru import logger

from models.mtgo_account import MtgoAccount
from helpers.permissions import has_bot_manager_role


class MtgoLinkCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        logger.info("MTGO link commands cog loaded")

    @staticmethod
    async def _send_link_result(ctx, status, detail, subject: str, conflict_extra: str = ""):
        """One response ladder for both the self-serve and admin link paths —
        ``subject`` is who got linked ('your Discord account' / a mention)."""
        if status == "empty":
            await ctx.followup.send("Please provide an MTGO username.", ephemeral=True)
        elif status == "conflict":
            await ctx.followup.send(
                f"That MTGO username is already linked to <@{detail}>.{conflict_extra}",
                ephemeral=True,
            )
        else:
            await ctx.followup.send(
                f"Linked {subject} to MTGO username **{detail}**.", ephemeral=True
            )

    @discord.slash_command(
        name="link_mtgo",
        description="Link your MTGO username so your draft match results can be auto-recorded",
    )
    async def link_mtgo(self, ctx, username: str):
        await ctx.defer(ephemeral=True)
        guild_id = ctx.guild.id if ctx.guild else None
        status, detail = await MtgoAccount.link(ctx.author.id, username, guild_id)
        await self._send_link_result(
            ctx, status, detail, "your Discord account",
            conflict_extra=" If it's really yours, ask an admin to reassign it with `/link_mtgo_for`.")

    @discord.slash_command(
        name="mtgo_whoami",
        description="Show which MTGO username is linked to your Discord account",
    )
    async def mtgo_whoami(self, ctx):
        await ctx.defer(ephemeral=True)
        row = await MtgoAccount.get_for_discord(ctx.author.id)
        if row is None:
            await ctx.followup.send(
                "You haven't linked an MTGO username yet. Use `/link_mtgo <username>`.",
                ephemeral=True,
            )
        else:
            await ctx.followup.send(
                f"Your linked MTGO username is **{row.mtgo_username}**.", ephemeral=True
            )

    @discord.slash_command(
        name="unlink_mtgo",
        description="Remove the MTGO username linked to your Discord account",
    )
    async def unlink_mtgo(self, ctx):
        await ctx.defer(ephemeral=True)
        removed = await MtgoAccount.unlink(ctx.author.id)
        await ctx.followup.send(
            "Removed your MTGO link." if removed else "You had no MTGO link to remove.",
            ephemeral=True,
        )

    @discord.slash_command(
        name="link_mtgo_for",
        description="(Admin) Link a player's MTGO username on their behalf",
    )
    @has_bot_manager_role()
    async def link_mtgo_for(self, ctx, player: discord.Member, username: str):
        await ctx.defer(ephemeral=True)
        guild_id = ctx.guild.id if ctx.guild else None
        status, detail = await MtgoAccount.link(player.id, username, guild_id)
        await self._send_link_result(ctx, status, detail, f"<@{player.id}>")


def setup(bot):
    bot.add_cog(MtgoLinkCommands(bot))
