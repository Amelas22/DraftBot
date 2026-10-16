"""Publish each guild's donation day page to DO Spaces, every 3 minutes while active.

Never touches money: it reads the ledgers and uploads a page. A failure is
logged and the next tick tries again; one guild failing never stops another.
The same tick also drives the two Discord channels (receipts and the leaderboard).
"""
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from helpers.digital_ocean_helper import DigitalOceanHelper
from services.donation_day import Event, gather_snapshot, get_event, tally
from services.donation_day_discord import get_state, post_receipts, update_leaderboard
from services.league_site_data import inject

PLACEHOLDER_ID = "donation-data"
PUBLISH_INTERVAL_SECONDS = 180
TEMPLATE = Path(__file__).resolve().parent / "donation_day_assets" / "donation_day.html"


def render(payload: dict[str, Any]) -> str:
    return inject(TEMPLATE.read_text(encoding="utf-8"), payload, PLACEHOLDER_ID)


_locks: dict[str, asyncio.Lock] = {}


def _page_url(event: Event) -> str:
    return DigitalOceanHelper().get_public_url(f"donations/{event['page_key']}/index.html")


async def publish_event(guild_id: str, event: Event, bot_id: str,
                        bot: Any = None) -> str | None:
    """One publish at a time per guild: setup/end handlers and the 3-minute loop all
    come through here, and two overlapping runs would post the same receipts twice."""
    async with _locks.setdefault(guild_id, asyncio.Lock()):
        return await _publish_event(guild_id, event, bot_id, bot)


async def _publish_event(guild_id: str, event: Event, bot_id: str,
                         bot: Any = None) -> str | None:
    """Build one event's page and upload it; with `bot`, also post receipts and refresh
    the leaderboard. Each step is isolated -- Discord failing never costs the page, and
    the page failing never costs the channels. The public URL, or None if the upload failed."""
    snapshot = await gather_snapshot(guild_id, event, bot_id)
    payload = tally(snapshot.credits, snapshot.outstanding, snapshot.names,
                    snapshot.anonymous, event, datetime.now())
    url: str | None = None
    try:
        helper = DigitalOceanHelper()
        result = await helper.upload_public_html(render(payload),
                                                 f"donations/{event['page_key']}", "index.html")
        if result.success and result.object_path is not None:
            url = helper.get_public_url(result.object_path)
        else:
            logger.warning(f"donation day: upload failed for guild {guild_id} "
                           f"(page {event['page_key']})")
    except Exception:
        logger.opt(exception=True).error(f"donation day: page upload raised for guild {guild_id}")
    if bot is not None:
        state = get_state(guild_id)
        if state is not None:
            try:
                state = await post_receipts(bot, guild_id, snapshot, state)
            except Exception:
                logger.opt(exception=True).error(
                    f"donation day: receipts failed for guild {guild_id}; retrying next tick")
            try:
                await update_leaderboard(bot, guild_id, payload, url or _page_url(event), state)
            except Exception:
                logger.opt(exception=True).error(
                    f"donation day: leaderboard failed for guild {guild_id}; retrying next tick")
    return url


async def publish_active(bot: Any) -> None:
    """One pass: every guild with an active donation day, each isolated from the rest."""
    user = getattr(bot, "user", None)
    if user is None:
        return
    for guild in bot.guilds:
        try:
            event = get_event(guild.id)
            if not event or not event.get("active"):
                continue
            await publish_event(str(guild.id), event, str(user.id), bot=bot)
        except Exception:
            logger.opt(exception=True).error(
                f"donation day: could not publish guild {guild.id}; retrying next tick")


async def run_donation_publisher(bot: Any) -> None:
    while True:
        try:
            await publish_active(bot)
        except Exception:
            logger.opt(exception=True).error("donation day: publisher pass failed")
        await asyncio.sleep(PUBLISH_INTERVAL_SECONDS)
