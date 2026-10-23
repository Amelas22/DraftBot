"""Once a minute, pick up usual weeks players saved from the scheduler page."""
from datetime import datetime, timezone
from typing_extensions import override

from discord.ext import commands, tasks
from loguru import logger

from helpers.digital_ocean_helper import DigitalOceanHelper
from services.availability_sync import AvailabilitySync
from services.scheduler_spaces import DoSchedulerSpaces


class SchedulerCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        helper = DigitalOceanHelper()
        self.sync = AvailabilitySync(DoSchedulerSpaces(helper))
        if helper.config_valid:
            self.poll.start()
        else:
            logger.warning("Scheduler poller disabled: Spaces is not configured")

    @override
    def cog_unload(self) -> None:
        self.poll.cancel()

    @tasks.loop(seconds=60)
    async def poll(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self.sync.poll_once(datetime.now(timezone.utc))
        except Exception:
            logger.exception("Scheduler poll failed")


def setup(bot: commands.Bot) -> None:
    bot.add_cog(SchedulerCog(bot))
