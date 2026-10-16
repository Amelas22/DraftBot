"""Publish each guild's donation day page to DO Spaces, every 3 minutes while active.

Never touches money: it reads the ledgers and uploads a page. A failure is
logged and the next tick tries again; one guild failing never stops another.
"""
import asyncio
from pathlib import Path
from typing import Any

from loguru import logger

from helpers.digital_ocean_helper import DigitalOceanHelper
from services.donation_day import Event, build_payload, get_event
from services.league_site_data import inject

PLACEHOLDER_ID = "donation-data"
PUBLISH_INTERVAL_SECONDS = 180
TEMPLATE = Path(__file__).resolve().parent / "donation_day_assets" / "donation_day.html"


def render(payload: dict[str, Any]) -> str:
    return inject(TEMPLATE.read_text(encoding="utf-8"), payload, PLACEHOLDER_ID)


async def publish_event(guild_id: str, event: Event, bot_id: str) -> str | None:
    """Build and upload one event's page. The public URL, or None if the upload failed."""
    html = render(await build_payload(guild_id, event, bot_id))
    helper = DigitalOceanHelper()
    result = await helper.upload_public_html(html, f"donations/{event['page_key']}", "index.html")
    if not result.success or result.object_path is None:
        logger.warning(f"donation day: upload failed for guild {guild_id}")
        return None
    return helper.get_public_url(result.object_path)


async def publish_active(bot: Any) -> None:
    """One pass: every guild with an active donation day, each isolated from the rest."""
    user = getattr(bot, "user", None)
    if user is None:
        return
    for guild in bot.guilds:
        event = get_event(guild.id)
        if not event or not event.get("active"):
            continue
        try:
            await publish_event(str(guild.id), event, str(user.id))
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
