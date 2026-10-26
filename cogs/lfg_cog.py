"""/lfg and the sticky "Find a team" board: hand out draftbot.fyi links."""
from datetime import datetime, timezone
from typing import Any

import discord
from discord.ext import commands, tasks
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import override

from config import get_config, scheduler_enabled, update_setting
from database.db_session import db_session
from helpers.tournament_channels import FIND_A_TEAM, ensure_channel
from helpers.utils import as_messageable, not_none, ui_button
from services.scheduler_links import issue_link
from services.team_finder import BoardTeam, board_teams
from services.team_finder_sync import run_reconcile

NOT_ENABLED = "Find a team isn't turned on in this server."
LINK_INTRO = ("Find teams that fit your week, or open your own. "
              "The link works for 7 days.")
LINK_FAILED = "Couldn't make your link just now. Try again in a minute."
BOARD_TITLE = "Find a team"
BOARD_TEXT = ("Looking for teammates for the next tournament? Press **Find a team** "
              "to see open teams ranked by how well your usual week fits theirs, "
              "ask to join one, or open your own.")


NO_TEAMS = "No open teams yet. Press **Find a team** to open one."
OPEN_TEAMS = "**Open teams**"
DESCRIPTION_MAX = 4096  # Discord's limit on an embed description
MORE_RESERVE = 64  # room kept for the "...and N more" line


def _safe(text: str) -> str:
    """Player-typed text as plain words: no formatting, links or pings."""
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text, ignore_links=False))


def _team_block(team: BoardTeam) -> str:
    lines = [f"**{_safe(team.captain_name)}'s team** · {team.filled}/{team.size} · "
             f"needs {team.size - team.filled}"]
    if team.others:
        lines.append(f"with {_safe(', '.join(team.others))}")
    if team.note:
        lines.append(f"*{_safe(team.note)}*")
    return "\n".join(lines)


def board_text(teams: list[BoardTeam]) -> str:
    """The board's description: the intro, then every open team that fits."""
    if not teams:
        return f"{BOARD_TEXT}\n\n{NO_TEAMS}"
    text = f"{BOARD_TEXT}\n\n{OPEN_TEAMS}"
    for shown, team in enumerate(teams):
        block = _team_block(team)
        if len(text) + len(block) + 2 > DESCRIPTION_MAX - MORE_RESERVE:
            return f"{text}\n\n…and {len(teams) - shown} more on the website."
        text += ("\n" if shown == 0 else "\n\n") + block
    return text


def board_embed(teams: list[BoardTeam]) -> discord.Embed:
    """The Find a team board, as first posted, refreshed and reposted by the sticky manager."""
    return discord.Embed(title=BOARD_TITLE, description=board_text(teams), color=discord.Color.green())


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
            try:
                await refresh_lfg_board(self.bot, guild_id)
            except Exception:
                logger.exception("lfg: couldn't refresh the board for guild {}", guild_id)

    @reconcile_loop.before_loop
    async def _before_reconcile(self) -> None:
        await self.bot.wait_until_ready()

    @discord.slash_command(name="lfg", description="Find a team for the next tournament")
    async def lfg(self, ctx: discord.ApplicationContext) -> None:
        await _respond(not_none(ctx.interaction))



# The description each board message was last drawn with, by message id, so the
# minute loop edits a board only when its list of teams has changed.
_rendered: dict[str, str] = {}


def forget_rendered_boards() -> None:
    _rendered.clear()


def remember_rendered(message: discord.Message) -> None:
    """Note what a freshly sent board shows, so the loop doesn't redraw it."""
    if message.embeds and message.embeds[0].description:
        _rendered[str(message.id)] = message.embeds[0].description


async def current_board_embed(session: AsyncSession, guild_id: str) -> discord.Embed:
    return board_embed(await board_teams(session, guild_id))


async def refresh_lfg_board(bot: discord.Client, guild_id: str) -> None:
    """Redraw the server's board in place when its open teams have changed."""
    from database.message_management import LFG_BOARD_VIEW_TYPE, Message

    async with db_session() as session:
        stickies = (await session.execute(
            select(Message).filter_by(guild_id=guild_id, is_sticky=True))).scalars()
        board = next((m for m in stickies
                      if (m.view_metadata or {}).get("view_type") == LFG_BOARD_VIEW_TYPE), None)
        if board is None:
            return
        channel_id, message_id = str(board.channel_id), str(board.message_id)
        embed = await current_board_embed(session, guild_id)
    if embed.description is None or _rendered.get(message_id) == embed.description:
        return
    channel = as_messageable(await bot.fetch_channel(int(channel_id)))
    message = await channel.fetch_message(int(message_id))
    await message.edit(embed=embed)
    _rendered[message_id] = embed.description


async def ensure_lfg_board(bot: discord.Client, guild: discord.Guild,
                           category: discord.CategoryChannel | None) -> None:
    """Make sure the server's find-a-team channel holds the sticky board.

    Called when a tournament opens registration. The channel is found or created
    like the other tournament channels; a board that's already there isn't posted again.
    """
    guild_id = str(guild.id)
    if not scheduler_enabled(guild_id):
        return
    # Local import: message_management pulls in views, which is heavy and cyclic here.
    from database.message_management import (
        LFG_BOARD_VIEW_TYPE, fetch_sticky_message, make_message_sticky)

    channel, _created = await ensure_channel(guild, get_config(guild_id), FIND_A_TEAM, category)
    update_setting(guild_id, f"tournament.{FIND_A_TEAM.setting}", str(channel.id))
    channel_id = str(channel.id)
    async with db_session() as session:
        existing = await fetch_sticky_message(channel_id, session)
    if existing is not None and (existing.view_metadata or {}).get("view_type") == LFG_BOARD_VIEW_TYPE:
        return
    async with db_session() as session:
        embed = await current_board_embed(session, guild_id)
    message = await channel.send(embed=embed, view=LfgBoardView())
    remember_rendered(message)
    await make_message_sticky(guild_id, channel_id, message, None, bot,
                              view_metadata={"view_type": LFG_BOARD_VIEW_TYPE})
    logger.info("lfg: posted the Find a team board in channel {}", channel_id)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(LfgCog(bot))
