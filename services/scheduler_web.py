"""The scheduler web app: static page plus the token-authenticated JSON API."""
import json
import re
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web

from services.availability import (
    MAX_FILE_BYTES,
    InvalidWeek,
    load_usual_week,
    parse_usual_week,
    save_usual_week,
    usual_week_doc,
)
from services.scheduler_links import LinkIdentity, resolve_token

TOKEN_LIMIT = 60
IP_LIMIT = 120
WINDOW = timedelta(minutes=1)
_LOOPBACK = frozenset({"127.0.0.1", "::1"})
_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

IDENTITY = "scheduler_identity"  # request key holding the LinkIdentity
_NOW: web.AppKey[Callable[[], datetime]] = web.AppKey("now")
_STATIC_DIR: web.AppKey[Path] = web.AppKey("static_dir")
_HITS: web.AppKey[dict[str, deque[datetime]]] = web.AppKey("hits")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _error(status: int, code: str) -> web.Response:
    return web.json_response({"error": code}, status=status)


def _client_ip(request: web.Request) -> str:
    peer = request.remote or ""
    if peer in _LOOPBACK:
        forwarded = request.headers.get("X-Forwarded-For", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return peer


def _over_limit(hits: dict[str, deque[datetime]], key: str, limit: int, now: datetime) -> bool:
    """Record a hit; True when this one exceeds ``limit`` per sliding minute."""
    window = hits.setdefault(key, deque())
    while window and now - window[0] >= WINDOW:
        window.popleft()
    if len(window) >= limit:
        return True
    window.append(now)
    return False


def _prune(hits: dict[str, deque[datetime]], now: datetime) -> None:
    for key in [k for k, w in hits.items() if not w or now - w[-1] >= WINDOW]:
        del hits[key]


@web.middleware
async def auth_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    if not request.path.startswith("/api/"):
        return await handler(request)
    now = request.app[_NOW]()
    hits = request.app[_HITS]
    if len(hits) > 1000:
        _prune(hits, now)
    if _over_limit(hits, "ip:" + _client_ip(request), IP_LIMIT, now):
        return _error(429, "rate_limited")
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    identity = await resolve_token(token.strip(), now) if scheme == "Bearer" and token.strip() else None
    if identity is None:
        return _error(401, "expired")
    if _over_limit(hits, "tok:" + token.strip(), TOKEN_LIMIT, now):
        return _error(429, "rate_limited")
    request[IDENTITY] = identity
    return await handler(request)


routes = web.RouteTableDef()


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@routes.get("/api/me")
async def get_me(request: web.Request) -> web.Response:
    who = request[IDENTITY]
    return web.json_response(
        {"user": who.user_id, "guild": who.guild_id, "view": who.view, "exp": _iso_z(who.expires_utc)}
    )


@routes.get("/api/usual-week")
async def get_usual_week(request: web.Request) -> web.Response:
    week = await load_usual_week(request[IDENTITY].user_id)
    if week is None:
        return _error(404, "not_set")
    return web.json_response(usual_week_doc(week))


@routes.put("/api/usual-week")
async def put_usual_week(request: web.Request) -> web.Response:
    if (request.content_length or 0) > MAX_FILE_BYTES:
        return _error(413, "too_large")
    raw = await request.content.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        return _error(413, "too_large")
    try:
        week = parse_usual_week(raw)
    except InvalidWeek:
        return _error(400, "bad_input")
    await save_usual_week(request[IDENTITY].user_id, week, request.app[_NOW]())
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
        headers={"Cache-Control": "no-cache"},
    )


def _safe_name(name: str, suffix: str) -> bool:
    return bool(_NAME.fullmatch(name)) and ".." not in name and name.endswith(suffix)


@routes.get("/")
@routes.get("/index.html")
async def get_index(request: web.Request) -> web.StreamResponse:
    return _static(request, "index.html")


@routes.get("/style.css")
async def get_style(request: web.Request) -> web.StreamResponse:
    return _static(request, "style.css")


@routes.get("/js/{name}")
async def get_script(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    if not _safe_name(name, ".js"):
        raise web.HTTPNotFound()
    return _static(request, f"js/{name}")


@routes.get("/fixtures/{name}")
async def get_fixture(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    if not _safe_name(name, ".json"):
        raise web.HTTPNotFound()
    return _static(request, f"fixtures/{name}")


def register_routes(app: web.Application, extra: web.RouteTableDef) -> None:
    """Add another module's routes to the same app."""
    app.add_routes(extra)


def build_app(
    now: Callable[[], datetime] = utc_now,
    static_dir: Path = Path("scheduler_page"),
) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app[_NOW] = now
    app[_STATIC_DIR] = static_dir
    app[_HITS] = {}
    register_routes(app, routes)
    return app
