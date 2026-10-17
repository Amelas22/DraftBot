"""/settings -- a player's own settings for this server, on one ephemeral message."""
import discord
from discord.ext import commands

from player_settings import load_settings
from player_settings_view import SettingsView, settings_embed


class SettingsCommands(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @discord.slash_command(name="settings", description="Your settings for this server")
    async def settings(self, ctx: discord.ApplicationContext) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.respond("Run /settings in a server.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        player_id, guild_id = str(ctx.author.id), str(guild.id)
        settings = await load_settings(player_id, guild_id)
        view = SettingsView(player_id, guild_id, guild.name, settings)
        await ctx.followup.send(embed=settings_embed(guild.name, settings),
                                view=view, ephemeral=True)
        view.opener = ctx.interaction


def setup(bot: discord.Bot) -> None:
    bot.add_cog(SettingsCommands(bot))
