"""Discord glue shared by the two rooms a booked time lives in: a team draft's
thread and a tournament match's.

Each owner keeps its own facts and copy; this is only what both do the same
way: wire a control message's buttons, hand out a Find a time link, and post a
booking announcement in the room.
"""
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

import discord
from loguru import logger

from config import scheduler_enabled
from helpers.matchup import TIME_LINK_FAILED, TIME_LINK_INTRO
from helpers.utils import as_messageable, not_none, reach_thread
from services.scheduler_links import issue_link

Press = Callable[[discord.Interaction, int], Coroutine[Any, Any, None]]


def add_press_button(view: discord.ui.View, subject_id: int, label: str, style: discord.ButtonStyle,
                     custom_id: str, press: Press) -> None:
    """A persistent button on ``view`` whose press calls ``press(interaction, subject_id)``."""
    button: discord.ui.Button[Any] = discord.ui.Button(label=label, style=style, custom_id=custom_id)

    async def callback(interaction: discord.Interaction) -> None:
        await press(interaction, subject_id)

    button.callback = callback
    view.add_item(button)


async def send_time_link(interaction: discord.Interaction, link_view: str, subject_id: int,
                         not_enabled: str, label: str) -> None:
    """Find a time: the presser's own 7-day draftbot.fyi link to this subject, privately.

    ``not_enabled`` is the owner's reply in a server without the scheduler;
    ``label`` names the subject in the log.
    """
    if interaction.guild_id is None or not scheduler_enabled(str(interaction.guild_id)):
        await interaction.response.send_message(not_enabled, ephemeral=True)
        return
    user_id = str(not_none(interaction.user).id)
    try:
        url = await issue_link(user_id, str(interaction.guild_id), link_view,
                               datetime.now(timezone.utc), subject=str(subject_id))
    except Exception:
        logger.exception(f"{label}: couldn't issue a link for {user_id}")
        await interaction.response.send_message(TIME_LINK_FAILED, ephemeral=True)
        return
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open Find a time", url=url))
    await interaction.response.send_message(TIME_LINK_INTRO, view=view, ephemeral=True)


async def post_announcement(bot: discord.Client, thread_id: int, content: str,
                            mentions: discord.AllowedMentions, label: str) -> bool:
    """Post a booking announcement in its room. False, logged, when the room
    can't be reached: its control message lives there too, so the caller skips
    re-rendering it. A refused send is logged and still returns True."""
    channel = await reach_thread(bot, thread_id)
    if channel is None:
        logger.warning(f"{label} room {thread_id} unreachable; booking not announced")
        return False
    try:
        await as_messageable(channel).send(content=content, allowed_mentions=mentions)
    except discord.HTTPException as e:
        logger.warning(f"Could not announce the booking for {label}: {e}")
    return True
