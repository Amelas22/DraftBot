"""Runs the scheduler web app inside the bot process."""
import asyncio

from aiohttp import web
from discord.ext import commands
from loguru import logger
from typing_extensions import override

from config import scheduler_host, scheduler_port
from helpers.matchup import Roster
from helpers.utils import resolve_display_name
from match_control_view import match_side_rosters
from services.scheduler_web import build_app
from services.team_draft import BookingChange
from services.team_finder import Notice
from services.team_finder_sync import notify
from team_draft_view import announce_booking, side_rosters


class SchedulerWebCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._runner: web.AppRunner | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._start_task = bot.loop.create_task(self._start())

    async def _display_name(self, guild_id: str, user_id: str) -> str:
        return await resolve_display_name(self.bot, guild_id, user_id)

    async def _notify(self, notice: Notice, guild_id: str) -> bool:
        return await notify(self.bot, notice, guild_id)

    async def _side_people(self, guild_id: str, team_draft_id: int) -> tuple[Roster, Roster]:
        return await side_rosters(self.bot, guild_id, team_draft_id)

    async def _match_people(self, guild_id: str, match_id: int) -> tuple[Roster, Roster] | None:
        return await match_side_rosters(self.bot, guild_id, match_id)

    async def _announce(self, team_draft_id: int, change: BookingChange) -> None:
        await announce_booking(self.bot, team_draft_id, change)

    async def _start(self) -> None:
        host, port = scheduler_host(), scheduler_port()
        runner = web.AppRunner(build_app(
            notify=self._notify,
            display_name=self._display_name,
            side_people=self._side_people,
            match_people=self._match_people,
            announce=self._announce,
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
