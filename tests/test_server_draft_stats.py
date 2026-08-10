"""
Unit tests for cogs.server_draft_stats
"""
import os
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import discord
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from database.db_session import AsyncSessionLocal
from database.models_base import Base
from models.draft_session import DraftSession
from models.match import MatchResult
from services.leaderboard_formatter import TIMEFRAME_DISPLAY
from services.leaderboard_service import get_timeframe_date

from cogs.server_draft_stats import (
    get_cube_draft_counts,
    rank_cubes,
    get_draft_fire_duration_stats,
    get_draft_completion_duration_stats,
    get_draft_start_hour_counts,
    format_duration,
    format_duration_field,
    format_hour_histogram,
    to_display_hour,
    DraftStatsView,
    DurationStats,
    DEFAULT_PERIOD,
    HISTOGRAM_BAR_WIDTH,
    PERIOD_MIN_DRAFTS,
)

GUILD = "guild_1"


@pytest_asyncio.fixture
async def test_db():
    """Bind the shared session factory to a temporary on-disk SQLite database."""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    temp_db.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    AsyncSessionLocal.configure(bind=engine)

    yield

    await engine.dispose()
    os.unlink(temp_db.name)


async def _seed(
    session_id: str,
    *,
    guild_id: str = GUILD,
    cube: str | None = "ArenaMax",
    completed: bool = True,
    session_stage: str = "completed",
    teams_start_time: datetime | None = None,
    draft_start_time: datetime | None = None,
) -> None:
    """Insert a DraftSession row with sensible defaults for these tests.

    draft_start_time is left unset unless explicitly given, since most tests
    don't care about it.
    """
    kwargs = dict(
        session_id=session_id,
        guild_id=guild_id,
        cube=cube,
        session_stage=session_stage,
        teams_start_time=teams_start_time or datetime.now(),
        sign_ups={},
        victory_message_id_draft_chat="999" if completed else None,
    )
    if draft_start_time is not None:
        kwargs["draft_start_time"] = draft_start_time

    async with AsyncSessionLocal() as db:
        async with db.begin():
            db.add(DraftSession(**kwargs))


@pytest.fixture
def eastern_host():
    """Pin the process's local zone, since to_display_hour reads naive DB
    timestamps as host-local - otherwise expected hours would vary by machine."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


_match_counter = 0


async def _seed_match_result(session_id: str, result_submitted_at: datetime | None, guild_id: str = GUILD) -> None:
    """Insert a MatchResult row for a session. result_submitted_at=None models
    an unreported match still in progress."""
    global _match_counter
    _match_counter += 1
    async with AsyncSessionLocal() as db:
        async with db.begin():
            db.add(MatchResult(
                session_id=session_id,
                match_number=_match_counter,
                player1_id="p1",
                player2_id="p2",
                winner_id="p1" if result_submitted_at else None,
                result_submitted_at=result_submitted_at,
                guild_id=guild_id,
            ))


class TestGetCubeDraftCounts:
    @pytest.mark.asyncio
    async def test_aggregates_completed_drafts_by_cube(self, test_db):
        await _seed("s1", cube="ArenaMax")
        await _seed("s2", cube="ArenaMax")
        await _seed("s3", cube="Vintage Cube")

        counts = await get_cube_draft_counts(GUILD, None)

        assert counts == Counter({"ArenaMax": 2, "Vintage Cube": 1})
        assert counts.total() == 3

    @pytest.mark.asyncio
    async def test_excludes_in_progress_sessions(self, test_db):
        """A draft that hasn't posted a victory message yet isn't 'completed'."""
        await _seed("s1", cube="ArenaMax", completed=True)
        await _seed("s2", cube="ArenaMax", completed=False, session_stage="pairings")

        counts = await get_cube_draft_counts(GUILD, None)

        assert counts == Counter({"ArenaMax": 1})
        assert counts.total() == 1

    @pytest.mark.asyncio
    async def test_excludes_abandoned_sessions(self, test_db):
        """Abandoned drafts never get a victory message (see check_and_post_victory_or_draw),
        so they're excluded the same way an in-progress draft is."""
        await _seed("s1", cube="ArenaMax", completed=True)
        await _seed("s2", cube="ArenaMax", completed=False, session_stage="abandoned")

        counts = await get_cube_draft_counts(GUILD, None)

        assert counts == Counter({"ArenaMax": 1})
        assert counts.total() == 1

    @pytest.mark.asyncio
    async def test_excludes_sessions_without_a_cube(self, test_db):
        await _seed("s1", cube="ArenaMax")
        await _seed("s2", cube=None)

        counts = await get_cube_draft_counts(GUILD, None)

        assert counts == Counter({"ArenaMax": 1})
        assert counts.total() == 1

    @pytest.mark.asyncio
    async def test_excludes_other_guilds(self, test_db):
        await _seed("s1", cube="ArenaMax", guild_id=GUILD)
        await _seed("s2", cube="ArenaMax", guild_id="some_other_guild")

        counts = await get_cube_draft_counts(GUILD, None)

        assert counts == Counter({"ArenaMax": 1})
        assert counts.total() == 1

    @pytest.mark.asyncio
    async def test_start_date_filters_out_older_drafts(self, test_db):
        now = datetime.now()
        await _seed("recent", cube="ArenaMax", teams_start_time=now)
        await _seed("old", cube="ArenaMax", teams_start_time=now - timedelta(days=30))

        counts = await get_cube_draft_counts(GUILD, now - timedelta(days=7))
        assert counts == Counter({"ArenaMax": 1})
        assert counts.total() == 1

        # With no start_date (lifetime), both are included.
        counts = await get_cube_draft_counts(GUILD, None)
        assert counts == Counter({"ArenaMax": 2})
        assert counts.total() == 2


class TestRankCubes:
    def test_sorts_descending_by_count(self):
        counts = Counter({"A": 2, "B": 5, "C": 3})
        assert rank_cubes(counts, min_drafts=0) == [("B", 5), ("C", 3), ("A", 2)]

    def test_drops_cubes_below_minimum(self):
        counts = Counter({"A": 5, "B": 2, "C": 1})
        assert rank_cubes(counts, min_drafts=2) == [("A", 5), ("B", 2)]

    def test_empty_when_nothing_meets_minimum(self):
        counts = Counter({"A": 1, "B": 1})
        assert rank_cubes(counts, min_drafts=2) == []


class TestGetDraftFireDurationStats:
    """min/avg/max/count are all computed via a single SQL aggregate query,
    never by pulling every row into Python - see the docstring on
    get_draft_fire_duration_stats. julianday() arithmetic in SQLite isn't
    bit-exact, so seconds are compared with pytest.approx."""

    @pytest.mark.asyncio
    async def test_none_when_no_data(self, test_db):
        assert await get_draft_fire_duration_stats(GUILD, None) is None

    @pytest.mark.asyncio
    async def test_single_draft(self, test_db):
        start = datetime(2026, 1, 1, 12, 0, 0)
        await _seed("s1", draft_start_time=start, teams_start_time=start + timedelta(minutes=5))

        stats = await get_draft_fire_duration_stats(GUILD, None)

        assert stats is not None
        assert stats.min_seconds == pytest.approx(300.0, abs=0.01)
        assert stats.avg_seconds == pytest.approx(300.0, abs=0.01)
        assert stats.max_seconds == pytest.approx(300.0, abs=0.01)
        assert stats.count == 1

    @pytest.mark.asyncio
    async def test_computes_arithmetic_mean(self, test_db):
        # Deliberately asymmetric (10, 20, 60) so avg (30) and median (20)
        # would disagree - this proves it's a true mean, not a median.
        start = datetime(2026, 1, 1, 12, 0, 0)
        for i, minutes in enumerate([10, 20, 60]):
            await _seed(f"s{i}", draft_start_time=start, teams_start_time=start + timedelta(minutes=minutes))

        stats = await get_draft_fire_duration_stats(GUILD, None)

        assert stats is not None
        assert stats.min_seconds == pytest.approx(600.0, abs=0.01)
        assert stats.avg_seconds == pytest.approx(1800.0, abs=0.01)
        assert stats.max_seconds == pytest.approx(3600.0, abs=0.01)
        assert stats.count == 3

    @pytest.mark.asyncio
    async def test_excludes_incomplete_sessions(self, test_db):
        """Timing is only meaningful for drafts that actually finished."""
        start = datetime(2026, 1, 1, 12, 0, 0)
        await _seed("s1", draft_start_time=start, teams_start_time=start + timedelta(minutes=5), completed=True)
        await _seed(
            "s2", draft_start_time=start, teams_start_time=start + timedelta(minutes=50),
            completed=False, session_stage="pairings",
        )

        stats = await get_draft_fire_duration_stats(GUILD, None)

        assert stats is not None
        assert stats.count == 1
        assert stats.avg_seconds == pytest.approx(300.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_excludes_sessions_missing_teams_start_time(self, test_db):
        """Malformed/legacy row with no teams_start_time can't yield a duration."""
        async with AsyncSessionLocal() as db:
            async with db.begin():
                db.add(DraftSession(
                    session_id="no_teams_start",
                    guild_id=GUILD,
                    cube="ArenaMax",
                    session_stage="completed",
                    sign_ups={},
                    draft_start_time=datetime(2026, 1, 1, 12, 0, 0),
                    teams_start_time=None,
                    victory_message_id_draft_chat="999",
                ))

        assert await get_draft_fire_duration_stats(GUILD, None) is None

    @pytest.mark.asyncio
    async def test_excludes_negative_durations(self, test_db):
        """Defensive: teams_start_time before draft_start_time is bad data, not a valid sample."""
        start = datetime(2026, 1, 1, 12, 0, 0)
        await _seed("bad", draft_start_time=start, teams_start_time=start - timedelta(minutes=5))

        assert await get_draft_fire_duration_stats(GUILD, None) is None

    @pytest.mark.asyncio
    async def test_start_date_filters_by_teams_start_time(self, test_db):
        now = datetime.now()
        await _seed("recent", draft_start_time=now - timedelta(minutes=10), teams_start_time=now)
        await _seed(
            "old", draft_start_time=now - timedelta(days=30, minutes=10),
            teams_start_time=now - timedelta(days=30),
        )

        stats = await get_draft_fire_duration_stats(GUILD, now - timedelta(days=7))
        assert stats is not None
        assert stats.count == 1
        assert stats.avg_seconds == pytest.approx(600.0, abs=0.01)

        stats = await get_draft_fire_duration_stats(GUILD, None)
        assert stats is not None
        assert stats.count == 2


class TestGetDraftCompletionDurationStats:
    """teams_start_time -> the LAST reported match result for that session
    (max result_submitted_at), via a join against match_results. This is a
    separate query shape from get_draft_fire_duration_stats (join vs a plain
    two-column diff), so these tests re-cover the aggregate math as well as
    the join-specific filters."""

    @pytest.mark.asyncio
    async def test_none_when_no_data(self, test_db):
        assert await get_draft_completion_duration_stats(GUILD, None) is None

    @pytest.mark.asyncio
    async def test_uses_the_last_reported_match_not_the_first(self, test_db):
        teams_start = datetime(2026, 1, 1, 12, 0, 0)
        await _seed("s1", teams_start_time=teams_start)
        await _seed_match_result("s1", teams_start + timedelta(minutes=30))
        await _seed_match_result("s1", teams_start + timedelta(minutes=50))  # last -> this one counts

        stats = await get_draft_completion_duration_stats(GUILD, None)

        assert stats is not None
        assert stats.avg_seconds == pytest.approx(3000.0, abs=0.01)
        assert stats.count == 1

    @pytest.mark.asyncio
    async def test_ignores_unreported_matches(self, test_db):
        """A match with no result yet shouldn't count as 'the last one', and
        shouldn't block the session from having a valid (partial) reading."""
        teams_start = datetime(2026, 1, 1, 12, 0, 0)
        await _seed("s1", teams_start_time=teams_start)
        await _seed_match_result("s1", teams_start + timedelta(minutes=30))
        await _seed_match_result("s1", None)  # still in progress

        stats = await get_draft_completion_duration_stats(GUILD, None)

        assert stats is not None
        assert stats.avg_seconds == pytest.approx(1800.0, abs=0.01)
        assert stats.count == 1

    @pytest.mark.asyncio
    async def test_excludes_sessions_with_no_reported_matches(self, test_db):
        teams_start = datetime(2026, 1, 1, 12, 0, 0)
        await _seed("s1", teams_start_time=teams_start)
        await _seed_match_result("s1", None)

        assert await get_draft_completion_duration_stats(GUILD, None) is None

    @pytest.mark.asyncio
    async def test_excludes_incomplete_sessions(self, test_db):
        teams_start = datetime(2026, 1, 1, 12, 0, 0)
        await _seed("s1", teams_start_time=teams_start, completed=True)
        await _seed_match_result("s1", teams_start + timedelta(minutes=45))
        await _seed("s2", teams_start_time=teams_start, completed=False, session_stage="pairings")
        await _seed_match_result("s2", teams_start + timedelta(minutes=90))

        stats = await get_draft_completion_duration_stats(GUILD, None)

        assert stats is not None
        assert stats.count == 1
        assert stats.avg_seconds == pytest.approx(2700.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_excludes_negative_durations(self, test_db):
        """Defensive: a match reported before teams_start_time is bad data, not a valid sample."""
        teams_start = datetime(2026, 1, 1, 12, 0, 0)
        await _seed("bad", teams_start_time=teams_start)
        await _seed_match_result("bad", teams_start - timedelta(minutes=5))

        assert await get_draft_completion_duration_stats(GUILD, None) is None

    @pytest.mark.asyncio
    async def test_computes_arithmetic_mean_across_sessions(self, test_db):
        # Deliberately asymmetric (10, 20, 60) so avg (30) and median (20)
        # would disagree - this proves it's a true mean, not a median.
        teams_start = datetime(2026, 1, 1, 12, 0, 0)
        for i, minutes in enumerate([10, 20, 60]):
            session_id = f"s{i}"
            await _seed(session_id, teams_start_time=teams_start)
            await _seed_match_result(session_id, teams_start + timedelta(minutes=minutes))

        stats = await get_draft_completion_duration_stats(GUILD, None)

        assert stats is not None
        assert stats.avg_seconds == pytest.approx(1800.0, abs=0.01)
        assert stats.count == 3

    @pytest.mark.asyncio
    async def test_start_date_filters_by_teams_start_time(self, test_db):
        now = datetime.now()
        await _seed("recent", teams_start_time=now - timedelta(minutes=45))
        await _seed_match_result("recent", now)
        await _seed("old", teams_start_time=now - timedelta(days=30, minutes=45))
        await _seed_match_result("old", now - timedelta(days=30))

        stats = await get_draft_completion_duration_stats(GUILD, now - timedelta(days=7))
        assert stats is not None
        assert stats.count == 1

        stats = await get_draft_completion_duration_stats(GUILD, None)
        assert stats is not None
        assert stats.count == 2


class TestFormatDuration:
    def test_seconds_only(self):
        assert format_duration(45) == "45s"

    def test_minutes_and_seconds(self):
        assert format_duration(125) == "2m 5s"

    def test_hours_and_minutes(self):
        assert format_duration(3900) == "1h 5m"

    def test_rounds_to_nearest_second(self):
        assert format_duration(59.6) == "1m 0s"


@pytest.mark.usefixtures("eastern_host")
class TestToDisplayHour:
    """Host pinned to Eastern, so stored timestamps are Eastern and DISPLAY_TZ
    (Pacific) is a 3-hour shift back."""

    def test_shifts_eastern_to_pacific(self):
        # 20:30 ET -> 17:30 PT
        assert to_display_hour(datetime(2026, 7, 25, 20, 30)) == 17

    def test_shift_holds_outside_daylight_saving(self):
        """Both zones change DST together, so the gap is still 3h in January."""
        assert to_display_hour(datetime(2026, 1, 15, 20, 30)) == 17

    def test_wraps_backwards_across_midnight(self):
        """01:00 ET is the previous day in Pacific, so it wraps to 22, not 0."""
        assert to_display_hour(datetime(2026, 7, 25, 1, 0)) == 22

    def test_midnight_eastern(self):
        assert to_display_hour(datetime(2026, 7, 25, 0, 0)) == 21


@pytest.mark.usefixtures("eastern_host")
class TestGetDraftStartHourCounts:
    @pytest.mark.asyncio
    async def test_empty_when_no_data(self, test_db):
        assert await get_draft_start_hour_counts(GUILD, None) == Counter()

    @pytest.mark.asyncio
    async def test_buckets_by_display_hour(self, test_db):
        # 20:xx ET all land in the 17:00 PT bucket; 22:00 ET lands in 19:00 PT.
        await _seed("s1", teams_start_time=datetime(2026, 7, 25, 20, 5))
        await _seed("s2", teams_start_time=datetime(2026, 7, 25, 20, 55))
        await _seed("s3", teams_start_time=datetime(2026, 7, 26, 22, 0))

        assert await get_draft_start_hour_counts(GUILD, None) == Counter({17: 2, 19: 1})

    @pytest.mark.asyncio
    async def test_excludes_incomplete_sessions(self, test_db):
        """Matches the cube counts and timing stats: only completed drafts."""
        await _seed("s1", teams_start_time=datetime(2026, 7, 25, 20, 0), completed=True)
        await _seed(
            "s2", teams_start_time=datetime(2026, 7, 25, 20, 0),
            completed=False, session_stage="pairings",
        )

        assert await get_draft_start_hour_counts(GUILD, None) == Counter({17: 1})

    @pytest.mark.asyncio
    async def test_excludes_other_guilds(self, test_db):
        await _seed("s1", teams_start_time=datetime(2026, 7, 25, 20, 0), guild_id=GUILD)
        await _seed("s2", teams_start_time=datetime(2026, 7, 25, 20, 0), guild_id="some_other_guild")

        assert await get_draft_start_hour_counts(GUILD, None) == Counter({17: 1})

    @pytest.mark.asyncio
    async def test_excludes_sessions_missing_teams_start_time(self, test_db):
        """A legacy row with no teams_start_time must not reach the conversion."""
        async with AsyncSessionLocal() as db:
            async with db.begin():
                db.add(DraftSession(
                    session_id="no_teams_start",
                    guild_id=GUILD,
                    cube="ArenaMax",
                    session_stage="completed",
                    sign_ups={},
                    teams_start_time=None,
                    victory_message_id_draft_chat="999",
                ))

        assert await get_draft_start_hour_counts(GUILD, None) == Counter()

    @pytest.mark.asyncio
    async def test_start_date_filters_out_older_drafts(self, test_db):
        now = datetime.now()
        await _seed("recent", teams_start_time=now)
        await _seed("old", teams_start_time=now - timedelta(days=30))

        recent_only = await get_draft_start_hour_counts(GUILD, now - timedelta(days=7))
        assert sum(recent_only.values()) == 1

        lifetime = await get_draft_start_hour_counts(GUILD, None)
        assert sum(lifetime.values()) == 2


class TestFormatHourHistogram:
    def test_no_data_message_when_empty(self):
        assert format_hour_histogram(Counter()) == "No draft start times available for this period."

    def test_lists_all_twenty_four_hours(self):
        """Quiet hours are empty rows, not omitted, so the day's shape is intact."""
        text = format_hour_histogram(Counter({17: 5}))
        hour_rows = [line for line in text.splitlines() if line[:2].isdigit()]

        assert len(hour_rows) == 24
        assert hour_rows[0].startswith("00")
        assert hour_rows[-1].startswith("23")

    def test_has_a_column_header_above_a_rule(self):
        text = format_hour_histogram(Counter({17: 5}))
        header, rule = text.splitlines()[1:3]

        assert header.startswith("HH")
        assert header.rstrip().endswith("#")
        assert set(rule) == {"─"}
        assert len(rule) == len(header)

    def test_busiest_hour_gets_a_full_width_bar(self):
        text = format_hour_histogram(Counter({17: 10, 18: 5}))
        peak_line = next(line for line in text.splitlines() if line.startswith("17"))

        assert peak_line.count("█") == HISTOGRAM_BAR_WIDTH

    def test_scales_other_hours_against_the_peak(self):
        text = format_hour_histogram(Counter({17: 12, 18: 6}))
        half_line = next(line for line in text.splitlines() if line.startswith("18"))

        assert half_line.count("█") == HISTOGRAM_BAR_WIDTH // 2

    def test_low_but_nonzero_hour_still_draws_a_block(self):
        """1 of 100 rounds to zero blocks, so it has to floor at one."""
        text = format_hour_histogram(Counter({10: 100, 3: 1}))
        thin_line = next(line for line in text.splitlines() if line.startswith("03"))

        assert thin_line.count("█") == 1

    def test_empty_hour_draws_no_blocks(self):
        text = format_hour_histogram(Counter({10: 100}))
        empty_line = next(line for line in text.splitlines() if line.startswith("04"))

        assert "█" not in empty_line
        assert empty_line.endswith("0")

    def test_counts_are_rendered(self):
        text = format_hour_histogram(Counter({17: 23}))
        peak_line = next(line for line in text.splitlines() if line.startswith("17"))

        assert peak_line.endswith("23")

    def test_fits_within_discord_embed_field_limit(self):
        """Discord rejects an embed field over 1024 chars, failing the command."""
        text = format_hour_histogram(Counter({hour: 9999 for hour in range(24)}))

        assert len(text) <= 1024


class TestPeriodsAndView:
    """The view wires one hardcoded button per period; styling is derived from
    each button's custom_id in one __init__ loop and disabling uses
    disable_all_items, so a new period touches two places (the min-drafts map
    and its button stub). These cover what would fail silently if missed."""

    def test_default_period_is_a_configured_period(self):
        """A typo here would KeyError on the command's primary path."""
        assert DEFAULT_PERIOD in PERIOD_MIN_DRAFTS

    def test_every_period_resolves_through_the_shared_timeframe_helpers(self):
        """Dates and labels are reused from the leaderboard machinery (#391);
        a period key outside that vocabulary would KeyError at display time."""
        for period in PERIOD_MIN_DRAFTS:
            assert period in TIMEFRAME_DISPLAY
            start = get_timeframe_date(period)
            assert (start is None) == (period == "lifetime")

    def test_cutoff_rises_with_period_length(self):
        ordered = ["14d", "30d", "90d", "lifetime"]
        assert list(PERIOD_MIN_DRAFTS) == ordered
        mins = [PERIOD_MIN_DRAFTS[p] for p in ordered]
        assert mins == sorted(mins)

    @pytest.mark.asyncio
    async def test_one_button_per_period(self):
        view = DraftStatsView(GUILD, DEFAULT_PERIOD)

        labels = {getattr(child, "label", None) for child in view.children}
        assert labels == {TIMEFRAME_DISPLAY[p] for p in PERIOD_MIN_DRAFTS}

    @pytest.mark.asyncio
    async def test_only_the_current_period_is_highlighted(self):
        """Catches a button whose custom_id doesn't carry its period."""
        view = DraftStatsView(GUILD, "90d")

        highlighted = [c for c in view.children if getattr(c, "style", None) == discord.ButtonStyle.primary]
        assert [c.label for c in highlighted] == [TIMEFRAME_DISPLAY["90d"]]

    @pytest.mark.asyncio
    async def test_timeout_disables_every_button(self):
        """Catches a new period missing its disable line in on_timeout."""
        view = DraftStatsView(GUILD, DEFAULT_PERIOD)
        view.message = AsyncMock()

        await view.on_timeout()

        assert all(child.disabled for child in view.children)


class TestFormatDurationField:
    def test_no_data_message_when_none(self):
        assert format_duration_field(None) == "No timing data available for this period."

    def test_renders_min_avg_max(self):
        stats = DurationStats(min_seconds=60, avg_seconds=300, max_seconds=3600, count=5)
        text = format_duration_field(stats)
        assert "Min: 1m 0s" in text
        assert "Avg: 5m 0s" in text
        assert "Max: 1h 0m" in text
