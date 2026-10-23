"""Runs the scheduler web app inside the bot process."""
import asyncio

from aiohttp import web
from discord.ext import commands
from loguru import logger
from typing_extensions import override

from config import scheduler_host, scheduler_port
from services.scheduler_web import build_app


class SchedulerWebCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._runner: web.AppRunner | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._start_task = bot.loop.create_task(self._start())

    async def _start(self) -> None:
        host, port = scheduler_host(), scheduler_port()
        runner = web.AppRunner(build_app())
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
