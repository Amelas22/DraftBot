"""/team_draft_board: put the sticky Set up a team draft board in #set-team-draft.

There is no natural event (like a tournament opening registration) to hang the
board on, so a bot manager posts it once; running the command again is harmless.
"""
from typing import Any

import discord
from discord.ext import commands
from loguru import logger

from config import get_config, scheduler_enabled, update_setting
from database.db_session import db_session
from helpers.permissions import has_bot_manager_role
from helpers.tournament_channels import SET_TEAM_DRAFT, ensure_channel
from team_draft_view import NOT_ENABLED
from team_draft_wizard import TeamDraftBoardView, board_embed

NO_GUILD = "Use this in a server."


async def ensure_team_draft_board(
    bot: discord.Client, guild: discord.Guild,
    category: discord.CategoryChannel | None = None,
    chosen: discord.TextChannel | None = None,
) -> tuple[Any, bool]:
    """(the set-team-draft channel, whether a board was posted just now).

    ``chosen`` wins when given; otherwise the channel is found or created like
    the tournament channels. A board that's already sticky there isn't posted
    again.
    """
    # Local import: message_management pulls in views, which is heavy and cyclic here.
    from database.message_management import (
        TEAM_DRAFT_BOARD_VIEW_TYPE, fetch_sticky_message, make_message_sticky)

    guild_id = str(guild.id)
    channel, _created = await ensure_channel(
        guild, get_config(guild_id), SET_TEAM_DRAFT, category, chosen)
    update_setting(guild_id, f"tournament.{SET_TEAM_DRAFT.setting}", str(channel.id))
    channel_id = str(channel.id)
    async with db_session() as session:
        existing = await fetch_sticky_message(channel_id, session)
    if (existing is not None
            and (existing.view_metadata or {}).get("view_type") == TEAM_DRAFT_BOARD_VIEW_TYPE):
        return channel, False
    message = await channel.send(embed=board_embed(), view=TeamDraftBoardView())
    await make_message_sticky(guild_id, channel_id, message, None, bot,
                              view_metadata={"view_type": TEAM_DRAFT_BOARD_VIEW_TYPE})
    logger.info("team draft: posted the Set up a team draft board in channel {}", channel_id)
    return channel, True


class TeamDraftCog(commands.Cog):
    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @discord.slash_command(name="team_draft_board",
                           description="Post the Set up a team draft board in #set-team-draft")
    @has_bot_manager_role()
    async def team_draft_board(self, ctx: discord.ApplicationContext) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.respond(NO_GUILD, ephemeral=True)
            return
        if not scheduler_enabled(str(guild.id)):
            await ctx.respond(NOT_ENABLED, ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        # The board goes in the channel the command was run from: a server
        # that already has a room for this names it by running the command
        # there, whatever the room is called. From a thread or anywhere else,
        # a new channel goes in that category, like the tournament channels.
        here = ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None
        category = here.category if here is not None else None
        try:
            channel, posted = await ensure_team_draft_board(self.bot, guild, category, here)
        except discord.HTTPException as e:
            logger.warning(f"team draft: couldn't set up the board in guild {guild.id}: {e}")
            await ctx.followup.send(f"Couldn't set up #{SET_TEAM_DRAFT.name}: {e}", ephemeral=True)
            return
        text = (f"Posted the Set up a team draft board in <#{channel.id}>." if posted
                else f"The Set up a team draft board is already in <#{channel.id}>.")
        await ctx.followup.send(text, ephemeral=True)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(TeamDraftCog(bot))
