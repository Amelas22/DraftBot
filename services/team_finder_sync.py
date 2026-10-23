"""Keeps forming teams in step with tournament registrations, and delivers their DMs."""
from datetime import datetime, timezone

import discord
from loguru import logger

from config import scheduler_enabled
from database.db_session import db_session
from notification_service import send_dm
from services import team_finder
from services.scheduler_http import utc_now
from services.scheduler_links import issue_link
from services.team_finder import Notice


async def notify(bot: discord.Bot, notice: Notice, guild_id: str) -> bool:
    """DM a notice, appending a fresh link when it carries one."""
    text = notice.text
    if notice.link_view is not None:
        link = await issue_link(notice.user_id, guild_id, notice.link_view, utc_now())
        text += "\n" + link
    return await send_dm(bot, notice.user_id, text)


async def run_reconcile(bot: discord.Bot, guild_id: str) -> None:
    """Reconcile one guild under the write lock, then send the resulting DMs."""
    if not scheduler_enabled(guild_id):
        return
    async with team_finder.WRITE_LOCK:
        async with db_session() as session:
            notices = await team_finder.reconcile_registered(
                session, guild_id, datetime.now(timezone.utc)
            )
            await session.commit()
    for notice in notices:
        try:
            delivered = await notify(bot, notice, guild_id)
        except Exception as exc:  # a DM must never fail the committed change
            logger.warning(f"Teams notice to {notice.user_id} raised: {exc!r}")
            continue
        if not delivered:
            logger.info(f"Teams notice to {notice.user_id} was not delivered")


async def reconcile_after_command(bot: discord.Bot, guild_id: object) -> None:
    """Hook for tournament commands: never lets a team-finder problem break the command."""
    try:
        await run_reconcile(bot, str(guild_id))
    except Exception:
        logger.exception("team finder reconcile failed for guild {}", guild_id)
