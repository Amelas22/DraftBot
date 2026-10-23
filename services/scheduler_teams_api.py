"""The find-a-team JSON API: team list and idempotent player actions."""
import json
import uuid
from datetime import datetime
from typing import Any

from aiohttp import web
from loguru import logger

from database.db_session import db_session
from models.team_finder import LfgAction
from services import team_finder
from services.availability import naive_utc
from services.scheduler_http import DISPLAY_NAME, NOTIFY, NOW, error, identity, read_body
from services.team_finder import ActionResult, teams_state

MAX_ACTION_BYTES = 4096
VIEW = "lfg"
ID_MAX = 32

routes = web.RouteTableDef()

class _BadInput(Exception):
    pass


def _text(body: dict[str, Any], key: str, limit: int, *, optional: bool = False) -> str:
    value = body.get(key)
    if value is None and optional:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise _BadInput
    return value


def _note(body: dict[str, Any]) -> str:
    return _text(body, "note", team_finder.NOTE_MAX, optional=True)


def _team(body: dict[str, Any]) -> str:
    return _text(body, "team_id", ID_MAX)


def _parse(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The action kind and its validated arguments."""
    kind = body.get("kind")
    if kind == "open_team":
        size = body.get("size", 3)
        if isinstance(size, bool) or not isinstance(size, int) or size not in team_finder.TEAM_SIZES:
            raise _BadInput
        return kind, {"size": size, "note": _note(body)}
    if kind == "request_join":
        return kind, {"team_id": _team(body), "note": _note(body)}
    if kind == "withdraw_request":
        return kind, {"team_id": _team(body)}
    if kind == "set_taking_requests":
        on = body.get("on")
        if not isinstance(on, bool):
            raise _BadInput
        return kind, {"team_id": _team(body), "on": on}
    raise _BadInput


async def _state(guild_id: str, user_id: str) -> dict[str, object]:
    async with db_session() as session:
        return await teams_state(session, guild_id, user_id)


def _view_refused(request: web.Request) -> web.Response | None:
    if identity(request).view != VIEW:
        return error(403, "wrong_view")
    return None


@routes.get("/api/teams")
async def get_teams(request: web.Request) -> web.Response:
    if (refused := _view_refused(request)) is not None:
        return refused
    who = identity(request)
    return web.json_response(await _state(who.guild_id, who.user_id))


async def _reply(ok: bool, code: str | None, guild_id: str, user_id: str) -> web.Response:
    return web.json_response(
        {"ok": ok, "code": code, "state": await _state(guild_id, user_id)}
    )


@routes.post("/api/teams/actions")
async def post_action(request: web.Request) -> web.Response:
    if (refused := _view_refused(request)) is not None:
        return refused
    raw = await read_body(request, MAX_ACTION_BYTES)
    if raw is None:
        return error(413, "too_large")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise _BadInput
        action_id = body.get("action_id")
        if not isinstance(action_id, str):
            raise _BadInput
        action_id = str(uuid.UUID(action_id))
        kind, args = _parse(body)
    except (ValueError, _BadInput):  # json and uuid errors are ValueErrors
        return error(400, "bad_input")

    who = identity(request)
    now: datetime = request.app[NOW]()
    name = ""
    if kind in ("open_team", "request_join"):
        name = await request.app[DISPLAY_NAME](who.guild_id, who.user_id)

    async with team_finder.WRITE_LOCK:
        async with db_session() as session:
            seen = await session.get(LfgAction, action_id)
            if seen is not None:
                if seen.user_id != who.user_id:
                    return error(400, "bad_input")
                stored = json.loads(str(seen.result_json))
                return await _reply(bool(stored["ok"]), stored["code"], who.guild_id, who.user_id)
            result = await _run(session, kind, args, who.guild_id, who.user_id, name, now)
            session.add(
                LfgAction(
                    action_id=action_id,
                    user_id=who.user_id,
                    result_json=json.dumps({"ok": result.ok, "code": result.code}),
                    done_utc=naive_utc(now),
                )
            )
            await session.commit()

    notify = request.app[NOTIFY]
    for notice in result.notices:
        try:
            delivered = await notify(notice, who.guild_id)
        except Exception as exc:  # a DM must never fail the committed action
            logger.warning(f"Teams notice to {notice.user_id} raised: {exc!r}")
            continue
        if not delivered:
            logger.info(f"Teams notice to {notice.user_id} was not delivered")
    return await _reply(result.ok, result.code, who.guild_id, who.user_id)


async def _run(
    session: Any,
    kind: str,
    args: dict[str, Any],
    guild_id: str,
    user_id: str,
    name: str,
    now: datetime,
) -> ActionResult:
    if kind == "open_team":
        return await team_finder.open_team(
            session, guild_id, user_id, name, args["size"], args["note"], now
        )
    if kind == "request_join":
        return await team_finder.request_join(
            session, guild_id, user_id, name, args["team_id"], args["note"], now
        )
    if kind == "withdraw_request":
        return await team_finder.withdraw_request(session, user_id, args["team_id"], now)
    return await team_finder.set_taking_requests(session, user_id, args["team_id"], args["on"], now)
