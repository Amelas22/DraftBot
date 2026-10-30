"""The scheduler web app: static page plus the token-authenticated JSON API."""
import re
from collections import deque
from collections.abc import Awaitable, Callable, Collection
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web

from config import scheduler_enabled, scheduler_trusted_proxies
from helpers.matchup import Roster
from services.availability import (
    MAX_FILE_BYTES,
    InvalidWeek,
    load_usual_week,
    parse_usual_week,
    save_usual_week,
    iso_z,
    usual_week_doc,
)
from services.scheduler_http import (
    ANNOUNCE,
    DISPLAY_NAME,
    IDENTITY,
    MATCH_ANNOUNCE,
    MATCH_PEOPLE,
    NOTIFY,
    NOW,
    SIDE_PEOPLE,
    error,
    identity,
    read_body,
    utc_now,
)
from services.scheduler_links import resolve_token
from services.scheduler_match_api import routes as match_routes
from services.scheduler_team_draft_api import routes as team_draft_routes
from services.scheduler_teams_api import routes as teams_routes
from services.team_draft import BookingChange
from services.team_finder import Notice

TOKEN_LIMIT = 60
IP_LIMIT = 120
WINDOW = timedelta(minutes=1)
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

_STATIC_DIR: web.AppKey[Path] = web.AppKey("static_dir")
_LIMITER: web.AppKey["RateLimiter"] = web.AppKey("limiter")
_ENABLED: web.AppKey[Callable[[str], bool]] = web.AppKey("enabled")
_TRUSTED: web.AppKey[frozenset[str]] = web.AppKey("trusted_proxies")
MAX_KEYS = 10_000
_REPO_PAGE_DIR = Path(__file__).resolve().parent.parent / "scheduler_page"


def client_ip(peer: str | None, forwarded: str, trusted: Collection[str]) -> str:
    """The caller's address. A trusted proxy appends the real peer last, so
    behind one the last X-Forwarded-For entry is the one a client cannot
    forge; any other peer is used as-is."""
    peer = peer or ""
    if peer in trusted:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return peer


class RateLimiter:
    """In-process sliding-window counters with bounded memory."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[datetime]] = {}
        self._last_prune: datetime | None = None

    def _prune(self, now: datetime) -> None:
        self._last_prune = now
        for key in [k for k, w in self._hits.items() if not w or now - w[-1] >= WINDOW]:
            del self._hits[key]

    def over_limit(self, key: str, limit: int, now: datetime) -> bool:
        """Record a hit; True when this one exceeds ``limit`` per sliding minute."""
        if self._last_prune is None or now - self._last_prune >= WINDOW:
            self._prune(now)
        if key not in self._hits and len(self._hits) >= MAX_KEYS:
            self._prune(now)
            while len(self._hits) >= MAX_KEYS:
                # dicts keep insertion order: drop the oldest key
                del self._hits[next(iter(self._hits))]
        window = self._hits.setdefault(key, deque())
        while window and now - window[0] >= WINDOW:
            window.popleft()
        if len(window) >= limit:
            return True
        window.append(now)
        return False


_PUBLIC: set[Handler] = set()


def public(handler: Handler) -> Handler:
    """Mark a route handler as not needing a token (the static page)."""
    _PUBLIC.add(handler)
    return handler


@web.middleware
async def auth_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Every route needs a valid bearer token unless it is marked ``public``."""
    if request.match_info.http_exception is not None or handler in _PUBLIC:
        return await handler(request)
    now = request.app[NOW]()
    limiter = request.app[_LIMITER]
    ip = client_ip(
        request.remote, request.headers.get("X-Forwarded-For", ""), request.app[_TRUSTED]
    )
    if limiter.over_limit("ip:" + ip, IP_LIMIT, now):
        return error(429, "rate_limited")
    scheme, _, rest = request.headers.get("Authorization", "").partition(" ")
    token = rest.strip()
    found = await resolve_token(token, now) if scheme == "Bearer" and token else None
    if found is None:
        return error(401, "expired")
    if not request.app[_ENABLED](found.guild_id):
        return error(403, "not_enabled")
    if limiter.over_limit("tok:" + token, TOKEN_LIMIT, now):
        return error(429, "rate_limited")
    request[IDENTITY] = found
    return await handler(request)


routes = web.RouteTableDef()


@routes.get("/api/me")
async def get_me(request: web.Request) -> web.Response:
    who = identity(request)
    return web.json_response(
        {"user": who.user_id, "guild": who.guild_id, "view": who.view, "exp": iso_z(who.expires_utc)}
    )


@routes.get("/api/usual-week")
async def get_usual_week(request: web.Request) -> web.Response:
    week = await load_usual_week(identity(request).user_id)
    if week is None:
        return error(404, "not_set")
    return web.json_response(usual_week_doc(week))


@routes.put("/api/usual-week")
async def put_usual_week(request: web.Request) -> web.Response:
    raw = await read_body(request, MAX_FILE_BYTES)
    if raw is None:
        return error(413, "too_large")
    try:
        week = parse_usual_week(raw)
    except InvalidWeek:
        return error(400, "bad_input")
    await save_usual_week(identity(request).user_id, week, request.app[NOW]())
    return web.json_response(usual_week_doc(week))


_STATIC_TYPES = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".json": "application/json",
}


def _static(request: web.Request, relative: str) -> web.StreamResponse:
    root = request.app[_STATIC_DIR]
    path = root / relative
    suffix = path.suffix
    if suffix not in _STATIC_TYPES or not path.is_file():
        raise web.HTTPNotFound()
    return web.Response(
        body=path.read_bytes(),
        content_type=_STATIC_TYPES[suffix],
        charset="utf-8",
        headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
        },
    )


def _safe_name(name: str, suffix: str) -> bool:
    return bool(_NAME.fullmatch(name)) and ".." not in name and name.endswith(suffix)


@routes.get("/")
@routes.get("/index.html")
@public
async def get_index(request: web.Request) -> web.StreamResponse:
    return _static(request, "index.html")


@routes.get("/style.css")
@public
async def get_style(request: web.Request) -> web.StreamResponse:
    return _static(request, "style.css")


@routes.get("/js/{name}")
@public
async def get_script(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    if not _safe_name(name, ".js"):
        raise web.HTTPNotFound()
    return _static(request, f"js/{name}")


@routes.get("/fixtures/{name}")
@public
async def get_fixture(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    if not _safe_name(name, ".json"):
        raise web.HTTPNotFound()
    return _static(request, f"fixtures/{name}")


async def _no_notify(notice: Notice, guild_id: str) -> bool:
    return False


async def _id_as_name(guild_id: str, user_id: str) -> str:
    return user_id


async def _nobody(guild_id: str, team_draft_id: int) -> tuple[Roster, Roster]:
    return Roster("Side A", ()), Roster("Side B", ())


async def _no_match(guild_id: str, match_id: int) -> tuple[Roster, Roster] | None:
    return None


async def _no_announce(team_draft_id: int, change: BookingChange) -> None:
    return None


def build_app(
    now: Callable[[], datetime] = utc_now,
    static_dir: Path = _REPO_PAGE_DIR,
    enabled: Callable[[str], bool] = scheduler_enabled,
    trusted_proxies: Collection[str] | None = None,
    notify: Callable[[Notice, str], Awaitable[bool]] = _no_notify,
    display_name: Callable[[str, str], Awaitable[str]] = _id_as_name,
    side_people: Callable[[str, int], Awaitable[tuple[Roster, Roster]]] = _nobody,
    announce: Callable[[int, BookingChange], Awaitable[None]] = _no_announce,
    match_people: Callable[[str, int], Awaitable[tuple[Roster, Roster] | None]] = _no_match,
    match_announce: Callable[[int, BookingChange], Awaitable[None]] = _no_announce,
) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app[NOW] = now
    app[_STATIC_DIR] = static_dir
    app[_ENABLED] = enabled
    app[_TRUSTED] = frozenset(
        scheduler_trusted_proxies() if trusted_proxies is None else trusted_proxies
    )
    app[_LIMITER] = RateLimiter()
    app[NOTIFY] = notify
    app[DISPLAY_NAME] = display_name
    app[SIDE_PEOPLE] = side_people
    app[ANNOUNCE] = announce
    app[MATCH_PEOPLE] = match_people
    app[MATCH_ANNOUNCE] = match_announce
    app.add_routes(routes)
    app.add_routes(teams_routes)
    app.add_routes(team_draft_routes)
    app.add_routes(match_routes)
    return app
