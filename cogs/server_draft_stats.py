import operator
from collections import Counter
from datetime import datetime
from typing import NamedTuple, Optional
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from loguru import logger
from sqlalchemy import and_, func, select

from database.db_session import db_session
from helpers.permissions import has_bot_manager_role
from helpers.utils import not_none
from services.leaderboard_formatter import TIMEFRAME_DISPLAY
from services.leaderboard_service import get_timeframe_date
from models.draft_session import DraftSession
from models.match import MatchResult

MAX_CUBES_SHOWN = 25

DEFAULT_PERIOD = "90d"

# Fixed rather than per-viewer, so every admin reads the same numbers off a
# server-wide stat. Pacific because it's already how this bot buckets draft
# activity by time - weekly_summary's league week and PlayerLimit's weekly limits
# both cut at midnight Monday Pacific (spelled "US/Pacific" at those sites).
DISPLAY_TZ = ZoneInfo("America/Los_Angeles")

# Character width of the longest histogram bar.
HISTOGRAM_BAR_WIDTH = 18


# Minimum completed drafts a cube needs in the period to be displayed, scaled to
# the period length so a one-off draft doesn't clutter the shorter views.
# Keys are the leaderboard timeframe vocabulary — start dates come from
# services.leaderboard_service.get_timeframe_date and labels from
# services.leaderboard_formatter.TIMEFRAME_DISPLAY, so this cog adds no new
# token->date implementation (the existing duplication is tracked in #391).
PERIOD_MIN_DRAFTS: dict[str, int] = {
    "14d": 4,
    "30d": 6,
    "90d": 8,
    "lifetime": 10,
}


def _completed_draft_conditions(guild_id: str, start_date: Optional[datetime]) -> list[object]:
    """The one definition of "completed draft in this guild/period".

    victory_message_id_draft_chat is the repo's standard completed-draft
    filter (bet_analysis, history_cog, stale_drafts, ...). It is
    intentionally NOT services.ledger_stats._is_completed: that predicate's
    legacy-import arm would add ~1200 pre-bot rows that lack cube and
    teams_start_time, which no section of this embed could display.
    """
    conditions: list[object] = [
        DraftSession.guild_id == guild_id,
        DraftSession.victory_message_id_draft_chat.isnot(None),
    ]
    if start_date is not None:
        conditions.append(DraftSession.teams_start_time >= start_date)
    return conditions


async def get_cube_draft_counts(guild_id: str, start_date: Optional[datetime]) -> Counter[str]:
    """Count completed drafts per cube for a guild, optionally since start_date.

    The GROUP BY runs in SQL so the app only ever receives one row per distinct
    cube, not one row per draft session.
    """
    conditions = _completed_draft_conditions(guild_id, start_date)
    conditions.append(DraftSession.cube.isnot(None))

    async with db_session() as session:
        stmt = (
            select(DraftSession.cube, func.count())
            .where(and_(*conditions))
            .group_by(DraftSession.cube)
        )
        result = await session.execute(stmt)
        rows = result.all()

    return Counter({cube: count for cube, count in rows})


def rank_cubes(counts: Counter[str], min_drafts: int) -> list[tuple[str, int]]:
    """Sort cubes by draft count descending, dropping any below min_drafts."""
    return sorted(
        ((cube, count) for cube, count in counts.items() if count >= min_drafts),
        key=operator.itemgetter(1),
        reverse=True,
    )


class DurationStats(NamedTuple):
    min_seconds: float
    avg_seconds: float
    max_seconds: float
    count: int


async def get_draft_fire_duration_stats(guild_id: str, start_date: Optional[datetime]) -> Optional[DurationStats]:
    """Min/avg/max seconds from draft_start_time (sign-up posted) to
    teams_start_time (ready check passed and teams created). A single SQL
    aggregate query does all the work - only the final numbers (never the
    underlying rows) reach Python.
    """
    start_column = DraftSession.draft_start_time
    end_column = DraftSession.teams_start_time
    conditions = _completed_draft_conditions(guild_id, start_date)
    conditions += [
        start_column.isnot(None),
        end_column.isnot(None),
        end_column >= start_column,
    ]

    # SQLite has no interval type; julianday() converts a datetime to a
    # fractional day count, so the difference * seconds-per-day is the gap
    # in seconds between the two timestamps.
    duration_expr = (func.julianday(end_column) - func.julianday(start_column)) * 86400.0

    async with db_session() as session:
        agg_stmt = select(
            func.min(duration_expr), func.avg(duration_expr), func.max(duration_expr), func.count()
        ).where(and_(*conditions))
        min_seconds, avg_seconds, max_seconds, count = (await session.execute(agg_stmt)).one()

    if not count:
        return None
    return DurationStats(float(min_seconds), float(avg_seconds), float(max_seconds), count)


async def get_draft_completion_duration_stats(guild_id: str, start_date: Optional[datetime]) -> Optional[DurationStats]:
    """Min/avg/max seconds from teams_start_time (draft started) to the
    last match result submitted for that draft - i.e. total time from teams
    being created to the whole event (drafting + every match) finishing.

    Unlike get_draft_fire_duration_stats, the end point isn't a plain
    DraftSession column: it's the max(result_submitted_at) per session, so
    this needs its own query joining against a per-session aggregate over
    match_results instead of reusing _get_duration_stats.
    """
    last_match = (
        select(
            MatchResult.session_id.label("session_id"),
            func.max(MatchResult.result_submitted_at).label("last_result_at"),
        )
        .where(MatchResult.result_submitted_at.isnot(None))
        .group_by(MatchResult.session_id)
        .subquery()
    )

    conditions = _completed_draft_conditions(guild_id, start_date)
    conditions += [
        DraftSession.teams_start_time.isnot(None),
        last_match.c.last_result_at >= DraftSession.teams_start_time,
    ]

    duration_expr = (
        func.julianday(last_match.c.last_result_at) - func.julianday(DraftSession.teams_start_time)
    ) * 86400.0

    async with db_session() as session:
        agg_stmt = (
            select(func.min(duration_expr), func.avg(duration_expr), func.max(duration_expr), func.count())
            .select_from(DraftSession)
            .join(last_match, last_match.c.session_id == DraftSession.session_id)
            .where(and_(*conditions))
        )
        min_seconds, avg_seconds, max_seconds, count = (await session.execute(agg_stmt)).one()

    if not count:
        return None
    return DurationStats(float(min_seconds), float(avg_seconds), float(max_seconds), count)


def to_display_hour(stored: datetime) -> int:
    """Hour of day (0-23) in DISPLAY_TZ for a naive timestamp read from the DB.

    These columns are written by datetime.now() on the bot host, so they hold
    host-local wall clock, not UTC. astimezone() reads a naive datetime as
    host-local, so it round-trips them without hardcoding the deployment's zone.
    Caveat: running against a fetched copy of the prod DB reads prod's timestamps
    in the dev box's zone, shifting the histogram.
    """
    return stored.astimezone(DISPLAY_TZ).hour


async def get_draft_start_hour_counts(guild_id: str, start_date: Optional[datetime]) -> Counter[int]:
    """Count completed drafts by the DISPLAY_TZ hour their teams were created.

    Buckets on teams_start_time, the moment the draft actually fired, so these
    counts sum to the same total as "Total Completed Drafts".

    Bucketing happens in Python rather than SQL because SQLite can't convert
    DST-aware between named zones, and grouping on a fixed hour offset would skew
    rows on the far side of a DST change. Only one column is selected, so the
    row-per-draft cost stays negligible.
    """
    conditions = _completed_draft_conditions(guild_id, start_date)
    conditions.append(DraftSession.teams_start_time.isnot(None))

    async with db_session() as session:
        stmt = select(DraftSession.teams_start_time).where(and_(*conditions))
        rows = (await session.execute(stmt)).all()

    return Counter(to_display_hour(started_at) for (started_at,) in rows)


def format_duration(seconds: float) -> str:
    """Render a duration in seconds as a compact human-readable string."""
    total_seconds = round(seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_duration_field(stats: Optional[DurationStats]) -> str:
    """Build the embed field text for the time-to-fire section."""
    if stats is None:
        return "No timing data available for this period."

    return (
        f"Min: {format_duration(stats.min_seconds)}\n"
        f"Avg: {format_duration(stats.avg_seconds)}\n"
        f"Max: {format_duration(stats.max_seconds)}"
    )


def format_hour_histogram(counts: Counter[int]) -> str:
    """Render 24 hourly buckets as a fixed-width bar chart for an embed field.

    Empty hours are listed too, so the shape of the day reads at a glance. Bars
    scale to the busiest hour, flooring at one block so an hour with drafts in it
    never renders empty.
    """
    if not counts:
        return "No draft start times available for this period."

    peak = max(counts.values())
    count_width = len(str(peak))
    # "HH" labels the hour column, "#" the count column; the bar column between
    # them is left unlabelled since the bars speak for themselves.
    label_width = 2 + 1 + HISTOGRAM_BAR_WIDTH

    lines = [
        f"{'HH':<{label_width}} {'#':>{count_width}}",
        "─" * (label_width + 1 + count_width),
    ]
    for hour in range(24):
        count = counts[hour]
        blocks = max(1, round(count / peak * HISTOGRAM_BAR_WIDTH)) if count else 0
        bar = "█" * blocks
        lines.append(f"{hour:02d} {bar:<{HISTOGRAM_BAR_WIDTH}} {count:>{count_width}}")

    # Fenced so Discord renders it monospaced and the columns line up.
    return "```\n" + "\n".join(lines) + "\n```"


def display_tz_label() -> str:
    """Current abbreviation for DISPLAY_TZ ("PDT" in summer, "PST" in winter)."""
    return datetime.now(DISPLAY_TZ).strftime("%Z")


async def build_stats_embed(guild_id: str, period: str) -> discord.Embed:
    """Build the guild-draft-stats embed for a single timeframe."""
    start_date = get_timeframe_date(period)
    min_drafts = PERIOD_MIN_DRAFTS[period]
    counts = await get_cube_draft_counts(guild_id, start_date)
    total_drafts = counts.total()
    fire_stats = await get_draft_fire_duration_stats(guild_id, start_date)
    drafting_stats = await get_draft_completion_duration_stats(guild_id, start_date)
    hour_counts = await get_draft_start_hour_counts(guild_id, start_date)

    embed = discord.Embed(
        title="Server Draft Stats",
        description=f"Timeframe: **{TIMEFRAME_DISPLAY[period]}** (min {min_drafts} completed drafts to appear)",
        color=discord.Color.blue(),
    )
    embed.add_field(name="Total Completed Drafts", value=str(total_drafts), inline=False)

    if total_drafts == 0:
        embed.add_field(name="Drafts per Cube", value="No completed drafts found for this period.", inline=False)
    else:
        ranked = rank_cubes(counts, min_drafts)
        if not ranked:
            embed.add_field(
                name="Drafts per Cube",
                value=f"No cube reached the minimum of {min_drafts} drafts for this period.",
                inline=False,
            )
        else:
            lines = [f"{i + 1}. **{cube}**: {count} drafts" for i, (cube, count) in enumerate(ranked[:MAX_CUBES_SHOWN])]
            if len(ranked) > MAX_CUBES_SHOWN:
                lines.append(f"...and {len(ranked) - MAX_CUBES_SHOWN} more")
            embed.add_field(name="Drafts per Cube", value="\n".join(lines), inline=False)

    embed.add_field(
        name="Time to Fire (sign-up → teams created)",
        value=format_duration_field(fire_stats),
        inline=False,
    )
    embed.add_field(
        name="Time to Finish (teams created → last match reported)",
        value=format_duration_field(drafting_stats),
        inline=False,
    )
    embed.add_field(
        name=f"Draft Starts by Hour ({display_tz_label()})",
        value=format_hour_histogram(hour_counts),
        inline=False,
    )

    return embed


class ServerDraftStatsCog(commands.Cog):
    """Cog exposing server-wide draft activity stats (completed drafts per cube)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        logger.info("Server draft stats cog loaded")

    @discord.slash_command(
        name="server_draft_stats",
        description="[Admin] View completed drafts per cube for this server",
    )
    @has_bot_manager_role()
    @discord.option(
        "timeframe",
        str,
        description="Stats window",
        choices=[discord.OptionChoice(name=TIMEFRAME_DISPLAY[p], value=p) for p in PERIOD_MIN_DRAFTS],
        default=DEFAULT_PERIOD,
        required=False,
    )
    async def server_draft_stats(self, ctx: discord.ApplicationContext, timeframe: str) -> None:
        """Show how many completed drafts each cube has had in the chosen timeframe.

        Timeframe is a command option rather than buttons on the reply: the
        reply is ephemeral, and a view's buttons outlive both its timeout and
        py-cord's ability to edit the message (the webhook handle is clobbered
        on first dispatch), leaving clickable buttons that silently fail.
        """
        await ctx.defer(ephemeral=True)

        guild_id = str(not_none(ctx.guild).id)

        try:
            embed = await build_stats_embed(guild_id, timeframe)
        except Exception as e:
            logger.error(f"Error fetching server draft stats: {e}")
            await ctx.followup.send(f"An error occurred: {e}", ephemeral=True)
            return

        await ctx.followup.send(embed=embed, ephemeral=True)


def setup(bot: commands.Bot) -> None:
    bot.add_cog(ServerDraftStatsCog(bot))
