"""The tournament-match JSON API: one match's sides and booking, and idempotent book/clear.

A ``tournament_match`` link names its match in the link's subject, and speaks the
same document as a team draft's, through the same handlers. Every committed
action is announced in the match's thread; an announcement that fails is logged
and never fails the booking.
"""
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession

from database.db_session import db_session
from helpers.matchup import matchup_state
from services.match_booking import MatchStore, book_match, clear_match, schedulable_match
from services.scheduler_http import MATCH_ANNOUNCE, MATCH_PEOPLE, identity
from services.scheduler_team_draft_api import LinkSubject, booking_doc, build_state, serve_action, serve_state

VIEW = "tournament_match"

routes = web.RouteTableDef()


async def _state(request: web.Request, match_id: int) -> dict[str, object] | None:
    """The ``draftbot.scheduler.team_draft`` v1 body for a match, or None if it has no two sides."""
    who = identity(request)
    # One read of the match's own row; the sides' names come from Discord after.
    async with db_session() as session:
        found = await schedulable_match(session, who.guild_id, match_id)
        current = await MatchStore(match_id).current(session)
        if found is None or current is None:
            return None
        row = found.match
        thread_id, booked_by = row.thread_id, row.booked_by_name
    rosters = await request.app[MATCH_PEOPLE](who.guild_id, match_id)
    if rosters is None:
        return None
    start, version, started = current
    matchup: dict[str, object] = {
        "id": match_id,
        "name": f"{rosters[0].label} vs {rosters[1].label}",
        "thread_url": f"https://discord.com/channels/{who.guild_id}/{thread_id}" if thread_id else None,
        "state": matchup_state(started),
    }
    booking = booking_doc(start, booked_by, version)
    return await build_state(request, who, matchup, booking, rosters)


async def _match_exists(session: AsyncSession, guild_id: str, match_id: int) -> bool:
    return await schedulable_match(session, guild_id, match_id) is not None


MATCH = LinkSubject(
    view=VIEW,
    key="match_id",
    label="Match",
    exists=_match_exists,
    book=book_match,
    clear=clear_match,
    state=_state,
    announce=MATCH_ANNOUNCE,
)


@routes.get("/api/tournament-match")
async def get_match(request: web.Request) -> web.Response:
    return await serve_state(request, MATCH)


@routes.post("/api/tournament-match/actions")
async def post_action(request: web.Request) -> web.Response:
    return await serve_action(request, MATCH)
