"""Which calendar, week and booking span a scheduler page uses."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.tournament import STAGE_SWISS, Tournament, TournamentMatch, TournamentRound
from services.availability import CELL_MINUTES, floor_half_hour
from services.booking import WINDOW_CELLS, BookingSpan, rolling_span
from services.round_weeks import (
    Calendar,
    WeekPage,
    current_round,
    horizon,
    pick_page,
    round_week,
)


@dataclass(frozen=True)
class Schedule:
    start_utc: datetime
    cells: int
    page: WeekPage | None
    span: BookingSpan

    @property
    def shape(self) -> Literal["round", "rolling"]:
        return "rolling" if self.page is None else "round"

    @property
    def end_utc(self) -> datetime:
        return self.start_utc + timedelta(minutes=CELL_MINUTES * self.cells)


def rolling_schedule(now: datetime) -> Schedule:
    span = rolling_span(now)
    return Schedule(span.start_utc, WINDOW_CELLS, None, span)


def _page_schedule(page: WeekPage, span: BookingSpan) -> Schedule:
    return Schedule(page.week.start_utc, page.week.cells, page, span)


def _calendar(tournament: Tournament) -> Calendar | None:
    if tournament.round1_deadline is None:
        return None
    return Calendar(tournament.round1_deadline, tournament.total_rounds)


async def guild_calendar(session: AsyncSession, guild_id: str) -> Calendar | None:
    row = (
        await session.execute(
            select(Tournament)
            .where(
                Tournament.guild_id == guild_id,
                Tournament.status == "active",
                Tournament.round1_deadline.is_not(None),
            )
            .order_by(Tournament.created_at.desc(), Tournament.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return None if row is None else _calendar(row)


async def team_draft_schedule(
    session: AsyncSession, guild_id: str, query: Mapping[str, str], now: datetime
) -> Schedule:
    cal = await guild_calendar(session, guild_id)
    current = None if cal is None else current_round(cal, now)
    if current is None:
        cal = None
    page = pick_page(cal, query, now, current)
    return _page_schedule(page, BookingSpan(floor_half_hour(now), horizon(cal, now)))


async def match_schedule(
    session: AsyncSession, match_id: int, query: Mapping[str, str], now: datetime
) -> Schedule:
    row = (
        await session.execute(
            select(Tournament, TournamentRound)
            .join(TournamentRound, TournamentRound.tournament_id == Tournament.id)
            .join(TournamentMatch, TournamentMatch.round_id == TournamentRound.id)
            .where(TournamentMatch.id == match_id)
        )
    ).first()
    if row is None:
        return rolling_schedule(now)
    tournament, rnd = row
    if rnd.stage != STAGE_SWISS or rnd.round_number > tournament.total_rounds:
        return rolling_schedule(now)
    cal = _calendar(tournament)
    page = pick_page(cal, query, now, rnd.round_number)
    if cal is None:
        return _page_schedule(page, rolling_span(now))
    week = round_week(cal, rnd.round_number)
    return _page_schedule(page, BookingSpan(week.start_utc, week.end_utc))


async def this_week_horizon(session: AsyncSession, guild_id: str, now: datetime) -> datetime:
    """Latest instant a usual week may be saved to: wide enough for any page's paintable week."""
    cal = await guild_calendar(session, guild_id)
    return max(horizon(cal, now), horizon(None, now))
