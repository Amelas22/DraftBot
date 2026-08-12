"""
Async HTTP client for the MTGO TradeBot ``serve`` API — the *physical ledger* / trade executor.

The serve is a token-protected internal service (bearer auth, loopback / Tailscale) that owns the
bot's real MTGO collection and executes trades. DraftBot is just another authed client: it POSTs
deposit / withdraw / trade jobs and polls ``/jobs/{id}`` to a terminal state, then applies the result
to its own (obligation) ledger.

Config via env ``MTGO_TRADEBOT_URL`` + ``MTGO_TRADEBOT_TOKEN``. The client stays **disabled** — every
method returns ``None`` — unless *both* are set, so nothing breaks on servers without the integration
(mirrors the "stay disabled unless the token is set" guard in services/mtgo_result_api.py).

Mirrors the aiohttp idiom in helpers/magicprotools_helper.py, plus a Bearer header + a timeout.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from loguru import logger

# On MTGO, event tickets are the currency. Depositing/withdrawing tix is just trading this "card".
EVENT_TICKET = "Event Ticket"


class MtgoTradeBotClient:
    """Thin async wrapper over the serve API. All methods return the parsed JSON dict on success,
    or ``None`` on any failure / when disabled (they never raise to the caller)."""

    def __init__(self, url: Optional[str] = None, token: Optional[str] = None, timeout: float = 20.0):
        self.url = (url or os.getenv("MTGO_TRADEBOT_URL") or "").rstrip("/")
        self.token = token or os.getenv("MTGO_TRADEBOT_TOKEN") or ""
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    def _get_session(self) -> aiohttp.ClientSession:
        """One shared session for connection reuse — job polling hits the serve every few
        seconds for minutes at a time, so per-call sessions would pay a fresh TCP handshake
        each poll. Lives for the process; aiohttp reclaims idle connections itself."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _call(self, method: str, path: str, *, json=None, params=None,
                    mark_ambiguous: bool = False):
        """One HTTP call. Returns the parsed body, or None on any DEFINITE failure
        (disabled, HTTP error status, connection never established).

        ``mark_ambiguous``: for POSTs whose delivery matters (they create serve jobs) —
        a timeout/reset AFTER the connection was made may mean the request landed and
        only the response was lost, so those return ``{"_ambiguous": True}`` instead of
        None, letting the caller run job-adoption recovery only when it's warranted."""
        if not self.enabled:
            logger.warning("MtgoTradeBotClient disabled (MTGO_TRADEBOT_URL / MTGO_TRADEBOT_TOKEN not set)")
            return None
        headers = {"Authorization": f"Bearer {self.token}"}
        full = f"{self.url}{path}"
        try:
            session = self._get_session()
            async with session.request(method, full, headers=headers, json=json, params=params) as resp:
                text = await resp.text()
                if resp.status < 200 or resp.status >= 300:
                    logger.warning(f"TradeBot {method} {path} -> HTTP {resp.status}: {text[:200]}")
                    return None
                if not text:
                    return {}
                try:
                    return await resp.json()
                except Exception:
                    return {"raw": text}
        except aiohttp.ClientConnectorError as e:  # connection refused / DNS — never delivered
            logger.error(f"TradeBot {method} {path} unreachable: {e}")
            return None
        except Exception as e:  # timeout / reset mid-flight — delivery unknown
            logger.error(f"TradeBot {method} {path} failed ambiguously: {e}")
            return {"_ambiguous": True} if mark_ambiguous else None

    # ---- reads ----
    async def health(self):
        """{ok, custodian, commit, reconnecting, queued, jobs} — connectivity + arm state."""
        return await self._call("GET", "/health")

    async def vault(self):
        """{available, custodian, tix, distinct, top[]} — used to reconcile physical == Σ wallets."""
        return await self._call("GET", "/vault")

    async def find_recent_job(self, job_type: str, mtgo_user: str, qty: int,
                              max_age_s: float = 120.0):
        """Recover a job whose POST response was lost: scan GET /jobs for the newest
        non-failed job of this type/user/qty created within ``max_age_s``. A POST that
        actually reached the serve created a job even if we never saw the 202 — adopting
        it here keeps the ledger attached to a trade that may still complete. Returns the
        job dict or None."""
        listing = await self._call("GET", "/jobs")
        if not listing:
            return None
        now = datetime.now(timezone.utc)
        for job in listing.get("jobs", []):  # serve lists newest first
            if job.get("type") != job_type or job.get("state") == "failed":
                continue
            if (job.get("user") or "").lower() != mtgo_user.lower():
                continue
            items = job.get("receive") if job_type == "deposit" else job.get("give")
            if not items or items[0].get("name") != EVENT_TICKET or items[0].get("qty") != qty:
                continue
            try:
                ts = datetime.fromisoformat(job.get("createdAt") or "")
                if now - ts > timedelta(seconds=max_age_s):
                    continue
            except ValueError:
                pass  # unparseable timestamp: still adopt (better than stranding a live trade)
            return job
        return None

    async def get_job(self, job_id: str):
        """One job's projection incl. its terminal ``state`` (queued|running|done|failed) + ``detail``."""
        return await self._call("GET", f"/jobs/{job_id}")

    # ---- jobs (each returns the 202 job dict, whose ``id`` you then poll) ----
    async def deposit(self, user: str, card: str, qty: int = 1, commit: bool = True, wait_minutes: int = 0):
        """Bot RECEIVES qty of a card FROM the user (a deposit into custody)."""
        return await self._call("POST", "/deposit", json={
            "user": user, "cards": [card], "qty": qty, "commit": commit, "waitMinutes": wait_minutes},
            mark_ambiguous=True)

    async def give(self, user: str, card: str, qty: int = 1, commit: bool = True, wait_minutes: int = 0):
        """Bot GIVES qty of a card TO the user (a withdrawal, or a lend)."""
        return await self._call("POST", "/request", json={
            "user": user, "cards": [card], "qty": qty, "commit": commit, "waitMinutes": wait_minutes},
            mark_ambiguous=True)

    # ---- tix convenience (currency == Event Ticket) ----
    async def deposit_tix(self, user: str, n: int, commit: bool = True, wait_minutes: int = 0):
        return await self.deposit(user, EVENT_TICKET, n, commit=commit, wait_minutes=wait_minutes)

    async def withdraw_tix(self, user: str, n: int, commit: bool = True, wait_minutes: int = 0):
        return await self.give(user, EVENT_TICKET, n, commit=commit, wait_minutes=wait_minutes)

    async def bot_tix(self) -> Optional[int]:
        """Physical tix the bot currently holds (for the reconciliation audit). None if unavailable."""
        v = await self.vault()
        if not v or not v.get("available"):
            return None
        return v.get("tix")


# Lazy module-level singleton (env is loaded by bot.py's load_dotenv() before cogs import).
_client: Optional[MtgoTradeBotClient] = None


def get_client() -> MtgoTradeBotClient:
    global _client
    if _client is None:
        _client = MtgoTradeBotClient()
    return _client
