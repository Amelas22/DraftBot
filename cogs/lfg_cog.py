"""/lfg and the pinned "Find a team" board: hand out draftbot.fyi links."""
from datetime import datetime, timezone
from typing import Any

import discord
from discord.ext import commands, tasks
from loguru import logger
from typing_extensions import override

from config import scheduler_enabled
from helpers.permissions import has_bot_manager_role
from helpers.utils import not_none, ui_button
from services.scheduler_links import issue_link
from services.team_finder_sync import run_reconcile

NOT_ENABLED = "Find a team isn't turned on in this server."
LINK_INTRO = ("Find teams that fit your week, or open your own. "
              "The link works for 7 days.")
LINK_FAILED = "Couldn't make your link just now. Try again in a minute."
BOARD_TITLE = "Find a team"
BOARD_TEXT = ("Looking for teammates for the next tournament? Press **Find a team** "
              "to see open teams ranked by how well your usual week fits theirs, "
              "ask to join one, or open your own.")


async def lfg_reply(guild_id: str, user_id: str,
                    now: datetime) -> tuple[str, discord.ui.View | None]:
    """The ephemeral reply shared by /lfg and the board button."""
    if not scheduler_enabled(guild_id):
        return NOT_ENABLED, None
    try:
        url = await issue_link(user_id, guild_id, "lfg", now)
    except Exception:
        logger.exception("lfg: couldn't issue a link for {}", user_id)
        return LINK_FAILED, None
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open Find a team", url=url))
    return LINK_INTRO, view


NO_GUILD = "Use this in a server."
POST_FAILED = "I couldn't post here. Check my permissions in this channel."


async def _respond(interaction: discord.Interaction) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message(NO_GUILD, ephemeral=True)
        return
    guild_id = str(interaction.guild_id)
    user_id = str(not_none(interaction.user).id)
    content, view = await lfg_reply(guild_id, user_id, datetime.now(timezone.utc))
    if view is None:
        await interaction.response.send_message(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, view=view, ephemeral=True)


class LfgBoardView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @ui_button(label="Find a team", style=discord.ButtonStyle.success,
               custom_id="lfg:open")
    async def open_button(self, _button: discord.ui.Button[Any],
                          interaction: discord.Interaction) -> None:
        await _respond(interaction)


class LfgCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self.reconcile_loop.start()

    @override
    def cog_unload(self) -> None:
        self.reconcile_loop.cancel()

    @tasks.loop(seconds=60)
    async def reconcile_loop(self) -> None:
        for guild in list(self.bot.guilds):
            guild_id = str(guild.id)
            try:
                if not scheduler_enabled(guild_id):
                    continue
                await run_reconcile(self.bot, guild_id)
            except Exception:
                logger.exception("lfg: reconcile failed for guild {}", guild_id)

    @reconcile_loop.before_loop
    async def _before_reconcile(self) -> None:
        await self.bot.wait_until_ready()

    @discord.slash_command(name="lfg", description="Find a team for the next tournament")
    async def lfg(self, ctx: discord.ApplicationContext) -> None:
        await _respond(not_none(ctx.interaction))

    @discord.slash_command(name="lfg_board",
                           description="Admin: post and pin the Find a team button")
    @has_bot_manager_role()
    async def lfg_board(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild_id is None:
            await ctx.respond(NO_GUILD, ephemeral=True)
            return
        if not scheduler_enabled(str(ctx.guild_id)):
            await ctx.respond(NOT_ENABLED, ephemeral=True)
            return
        embed = discord.Embed(title=BOARD_TITLE, description=BOARD_TEXT,
                              color=discord.Color.green())
        try:
            message = await ctx.channel.send(embed=embed, view=LfgBoardView())
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("lfg: couldn't post the board in channel {}", ctx.channel_id)
            await ctx.respond(POST_FAILED, ephemeral=True)
            return
        try:
            await message.pin()
        except discord.HTTPException:
            logger.warning("lfg: couldn't pin the board in channel {}", ctx.channel_id)
            await ctx.respond("Posted, but I couldn't pin it.", ephemeral=True)
            return
        await ctx.respond("Posted and pinned.", ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(LfgCog(bot))
