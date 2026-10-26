"""What every scheduler API module shares: app keys, the caller's identity,
bounded body reads and error replies."""
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from aiohttp import web

from helpers.matchup import Roster
from services.scheduler_links import LinkIdentity
from services.team_draft import BookingChange
from services.team_finder import Notice

IDENTITY = "scheduler_identity"  # request key holding the LinkIdentity
NOW: web.AppKey[Callable[[], datetime]] = web.AppKey("now")
NOTIFY: web.AppKey[Callable[[Notice, str], Awaitable[bool]]] = web.AppKey("notify")
DISPLAY_NAME: web.AppKey[Callable[[str, str], Awaitable[str]]] = web.AppKey("display_name")
# (guild id, team draft id) -> both sides as the guild has them now.
SIDE_PEOPLE: web.AppKey[Callable[[str, int], Awaitable[tuple[Roster, Roster]]]] = web.AppKey(
    "side_people"
)
# (team draft id, committed change) -> post it in the matchup's thread.
ANNOUNCE: web.AppKey[Callable[[int, BookingChange], Awaitable[None]]] = web.AppKey("announce")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def error(status: int, code: str) -> web.Response:
    return web.json_response({"error": code}, status=status)


def identity(request: web.Request) -> LinkIdentity:
    """Who the request's bearer token belongs to (set by the auth middleware)."""
    found: LinkIdentity = request[IDENTITY]
    return found


async def read_body(request: web.Request, limit: int) -> bytes | None:
    """The request body, or None when it is larger than ``limit`` bytes."""
    if (request.content_length or 0) > limit:
        return None
    raw = b""
    while len(raw) <= limit:
        chunk = await request.content.read(limit + 1 - len(raw))
        if not chunk:
            break
        raw += chunk
    return raw if len(raw) <= limit else None
