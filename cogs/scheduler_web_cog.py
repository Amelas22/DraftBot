"""Runs the scheduler web app inside the bot process."""
import asyncio
from functools import partial

from aiohttp import web
from discord.ext import commands
from loguru import logger
from typing_extensions import override

from config import scheduler_host, scheduler_port
from helpers.utils import resolve_display_name
from match_control_view import match_side_rosters
from services.scheduler_web import build_app
from services.team_finder_sync import notify
from team_draft_view import announce_booking, side_rosters


class SchedulerWebCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._runner: web.AppRunner | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._start_task = bot.loop.create_task(self._start())

    async def _start(self) -> None:
        host, port = scheduler_host(), scheduler_port()
        bot = self.bot
        runner = web.AppRunner(build_app(
            notify=partial(notify, bot),
            display_name=partial(resolve_display_name, bot),
            side_people=partial(side_rosters, bot),
            match_people=partial(match_side_rosters, bot),
            announce=partial(announce_booking, bot),
        ))
        try:
            await runner.setup()
            await web.TCPSite(runner, host, port).start()
        except BaseException as exc:
            await runner.cleanup()
            if isinstance(exc, asyncio.CancelledError):
                raise
            logger.error(f"Scheduler web app could not start on {host}:{port}: {exc!r}")
            return
        self._runner = runner
        logger.info(f"Scheduler web app listening on http://{host}:{port}")

    @override
    def cog_unload(self) -> None:
        if self._start_task is not None:
            self._start_task.cancel()
        runner, self._runner = self._runner, None
        if runner is not None:
            self.bot.loop.create_task(runner.cleanup())


def setup(bot: commands.Bot) -> None:
    bot.add_cog(SchedulerWebCog(bot))
