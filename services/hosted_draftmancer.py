"""Client for the self-hosted Draftmancer fork's control API.

The fork exposes /api/hosted/sessions/<id>/... behind a bearer token. A session
is named on the wire "DB" + draft_id, the id the website already uses.

Three failures are told apart because callers act on them differently:
HostedUnavailable (the API could not be reached or answered 5xx -- retry later),
HostedRefused (it answered, and said no -- a 4xx the caller must handle or a bug),
and HostedLogStopped (the draft was stopped and will never have a log).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp

REQUEST_TIMEOUT_SECONDS = 5

# Draftmancer caps a room here; the pre-hosted manager set the same value.
MAX_PLAYERS = 10


class HostedUnavailable(Exception):
    """The hosted API did not answer, or answered with a server error."""


class HostedRefused(Exception):
    """The hosted API answered with a client error (401, 404, 409, 400...)."""

    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"hosted API refused with {status}: {body!r}")
        self.status = status
        self.body = body


class HostedLogStopped(Exception):
    """The draft was stopped before it finished (410): there is no log, ever.

    Distinct from fetch_log returning None, which only means "not yet".
    """


@dataclass(frozen=True)
class HostedSeat:
    name: str
    user_id: str
    connected: bool
    disconnected_since: float | None  # Epoch seconds; None while connected or never connected.
    ready: str  # "unknown" | "ready" | "not-ready"


@dataclass(frozen=True)
class HostedStatus:
    stage: str  # "gathering" | "drafting" | "ended" | "stopped"
    seats: list[HostedSeat]
    pick_number: int | None


def wire_id(draft_id: str) -> str:
    return "DB" + draft_id


def build_session_spec(
    draft_id: str,
    players: list[str],
    cube_id: str,
    packs_per_player: int,
    cards_per_pack: int,
    pick_timer: int,
    unlock_timer_minutes: int,
    log_webhook: str | None = None,
) -> dict[str, Any]:
    """The room settings DraftSetupManager.update_draft_settings applies today.

    Colour balance, log recipients ("delayed"), personal logs and
    ownerIsPlayer=False are the hosted fork's own defaults, so they are not sent.
    """
    return {
        "sessionID": wire_id(draft_id),
        "players": players,
        "cubeCobraID": cube_id,
        "settings": {
            "teamDraft": True,
            "pickTimer": pick_timer,
            "boostersPerPlayer": packs_per_player,
            "cardsPerBooster": cards_per_pack,
            "draftLogUnlockTimer": unlock_timer_minutes,
            "maxPlayers": MAX_PLAYERS,
        },
        "logWebhook": log_webhook,
    }


def _seat(raw: dict[str, Any]) -> HostedSeat:
    since = raw.get("disconnectedSince")
    return HostedSeat(
        name=raw["name"],
        user_id=raw["userID"],
        connected=bool(raw["connected"]),
        disconnected_since=None if since is None else since / 1000,
        ready=raw["ready"],
    )


class HostedDraftmancer:
    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._http: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            )
        return self._http

    async def _call(
        self, method: str, draft_id: str, tail: str = "", body: Any = None
    ) -> tuple[int, Any]:
        """One request; returns (status, json body or None). Raises HostedUnavailable on 5xx or no answer."""
        url = f"{self._base_url}/api/hosted/sessions/{wire_id(draft_id)}{tail}"
        try:
            async with self._session().request(method, url, json=body) as response:
                payload: Any = None
                if response.content_type == "application/json":
                    payload = await response.json()
                status = response.status
        except (aiohttp.ClientError, TimeoutError) as e:
            raise HostedUnavailable(f"{method} {url}: {e!r}") from e
        if status >= 500:
            raise HostedUnavailable(f"{method} {url}: {status} {payload!r}")
        return status, payload

    async def _ok(self, method: str, draft_id: str, tail: str = "", body: Any = None) -> Any:
        status, payload = await self._call(method, draft_id, tail, body)
        if status >= 400:
            raise HostedRefused(status, payload)
        return payload

    async def create(self, spec: dict[str, Any]) -> None:
        """Create the room, or confirm an identical one. A different room under the id is a 409 HostedRefused."""
        await self._ok("PUT", spec["sessionID"].removeprefix("DB"), body=spec)

    async def set_players(self, draft_id: str, names: list[str]) -> None:
        await self._ok("PUT", draft_id, "/players", {"players": names})

    async def set_seating(self, draft_id: str, names: list[str]) -> None:
        await self._ok("PUT", draft_id, "/seating", {"order": names})

    async def status(self, draft_id: str) -> HostedStatus:
        raw = await self._ok("GET", draft_id)
        return HostedStatus(
            stage=raw["stage"],
            seats=[_seat(s) for s in raw["seats"]],
            pick_number=raw["pickNumber"],
        )

    async def start(self, draft_id: str) -> list[str]:
        """Start the draft. Returns the names of seats nobody has taken yet; empty when it started."""
        status, payload = await self._call("POST", draft_id, "/start")
        if status == 409 and isinstance(payload, dict) and "missing" in payload:
            return list(payload["missing"])
        if status >= 400:
            raise HostedRefused(status, payload)
        return []

    async def ready_check(self, draft_id: str) -> None:
        await self._ok("POST", draft_id, "/ready-check")

    async def pause(self, draft_id: str) -> None:
        await self._ok("POST", draft_id, "/pause")

    async def resume(self, draft_id: str) -> None:
        await self._ok("POST", draft_id, "/resume")

    async def stop(self, draft_id: str) -> None:
        await self._ok("POST", draft_id, "/stop")

    async def release_log(self, draft_id: str) -> None:
        await self._ok("POST", draft_id, "/release-log")

    async def replace_with_bot(self, draft_id: str, name: str) -> None:
        await self._ok("POST", draft_id, f"/seats/{quote(name, safe='')}/replace-with-bot")

    async def remove_user(self, draft_id: str, user_id: str) -> None:
        await self._ok("DELETE", draft_id, f"/users/{quote(user_id, safe='')}")

    async def fetch_log(self, draft_id: str) -> dict[str, Any] | None:
        """The full draft log; None until the draft ends. HostedLogStopped for a stopped draft."""
        status, payload = await self._call("GET", draft_id, "/log")
        if status == 404:
            return None
        if status == 410:
            raise HostedLogStopped(draft_id)
        if status >= 400:
            raise HostedRefused(status, payload)
        return payload
