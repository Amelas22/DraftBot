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
from typing import Any, Optional

import aiohttp
from loguru import logger

# How long a trade offer stands before the serve gives up. Mirrors
# helpers.money_gate.DEFAULT_WAIT_MINUTES, which renders the same figure to
# players; importing that here would be a cycle (money_gate imports this).
DEFAULT_WAIT_MINUTES = 10

# On MTGO, event tickets are the currency. Depositing/withdrawing tix is just trading this "card".
EVENT_TICKET = "Event Ticket"

# What the serve will move in ONE trade. An order above this comes back split
# across several jobs, and settling a split order correctly -- booking each
# trade's own cards, surviving a scan that sees only some of them, refunding
# only what is really owed -- is a materially harder problem than settling one.
# So orders are REFUSED above this line rather than split, and every path that
# creates one checks first.
#
# It mirrors the serve's own MaxCardsPerTrade and cannot be read from the API,
# so it is configuration on this side: set MTGO_MAX_CARDS_PER_TRADE to match if
# the serve's limit changes. Too low only refuses orders that would have worked;
# too high lets a split through, which every caller then treats as unbookable
# and holds rather than refunds.
_DEFAULT_MAX_CARDS_PER_TRADE = 10


def max_cards_per_trade() -> int:
    """Read when ASKED, never at import.

    A module-level `os.getenv` is evaluated the first time something imports
    this module, and what imports it first is not something this module can
    see: bot.py loads the .env at line 41, but its line-7 import of
    database.message_management pulls this module in transitively before that.
    The constant then froze at its default while the environment said something
    else, and the bot refused orders the serve would happily have taken.
    """
    raw = os.getenv("MTGO_MAX_CARDS_PER_TRADE")
    try:
        return int(raw) if raw else _DEFAULT_MAX_CARDS_PER_TRADE
    except ValueError:
        logger.warning("MTGO_MAX_CARDS_PER_TRADE is not a number ({!r}); using {}",
                       raw, _DEFAULT_MAX_CARDS_PER_TRADE)
        return _DEFAULT_MAX_CARDS_PER_TRADE


def too_large(count: int) -> bool:
    """Would an order of this many cards have to be split?"""
    return count > max_cards_per_trade()


def _settle_body(user: str, card: str | None, qty: int | None,
                 commit: bool, wait_minutes: int) -> dict[str, Any]:
    """Request body shared by /return and /withdraw — the same settle in two directions.

    Three shapes the serve understands: no card at all settles everything open; a bare name
    settles every copy of it; name+qty settles part of a position, allocated oldest-first.
    """
    body: dict[str, Any] = {"user": user, "commit": commit, "waitMinutes": wait_minutes}
    if card and qty:
        body["items"] = [{"name": card, "qty": qty}]
    elif card:
        body["cards"] = [card]
    return body


def jobs_from(resp: "dict[str, Any] | None", total: int) -> list[tuple[str, int]]:
    """``[(job_id, amount)]`` for a serve response — one entry normally, several when the
    serve split the order across trades. ``total`` is what the caller asked for.

    MTGO caps a trade at a few hundred cards, so an order above the serve's limit comes
    back as ``{"batched": true, "batches": [...]}`` with **no top-level id**. That omission
    is deliberate on both sides: a caller that reads only ``id`` would poll one batch, book
    its share, and silently drop the rest — under-crediting a player with no error anywhere.
    Returning [] for any shape we don't recognise keeps that failure loud.

    A single job books ``total``: the caller's number is the authority for the whole order,
    and re-deriving it from the response would make a thin or unexpected body book zero.
    Only a split needs per-batch numbers, because only then does one job move less than the
    whole order.

    A split whose parts don't sum to ``total`` is REFUSED (``[]``). Some of the order would
    otherwise be recorded against no job at all — for a withdraw that means tix sitting in
    in-flight with nothing able to resolve them, which is the one way this stranding a
    player's money.
    """
    if not resp:
        return []

    if resp.get("batched"):
        jobs = [(b["id"],
                 sum(int(i.get("qty") or 0)
                     for i in (b.get("give") or []) + (b.get("receive") or [])))
                for b in (resp.get("batches") or []) if b.get("id")]
        booked = sum(n for _, n in jobs)
        if not jobs or booked != total:
            logger.error(f"serve split {total} into {len(jobs)} batch(es) totalling {booked}"
                         f" — refusing rather than booking a partial order")
            return []
        return jobs
    if resp.get("id"):
        return [(resp["id"], total)]
    return []


def _order_body(user: str, cards, qty: int, commit: bool,
                wait_minutes: int) -> dict[str, Any]:
    """Request body for an order that MOVES cards -- /deposit and /borrow.

    Three shapes, so the caller expresses what it actually has:
      * ``"Swamp"``                       -- one name, ``qty`` copies
      * ``["Swamp", "Island"]``           -- several names, ``qty`` copies of EACH
      * ``[{"name": "Swamp", "qty": 4}]`` -- per-card amounts (the serve's items[])

    The third sends ``items[]`` and NO ``qty``: the serve rejects both together because
    they disagree about what qty means, and being refused is better than moving a
    different number of cards than intended.
    """
    body: dict[str, Any] = {"user": user, "commit": commit, "waitMinutes": wait_minutes}
    if isinstance(cards, str):
        body["cards"], body["qty"] = [cards], qty
    elif cards and isinstance(cards[0], dict):
        body["items"] = [{"name": c["name"], "qty": int(c.get("qty") or 1)} for c in cards]
    else:
        body["cards"], body["qty"] = list(cards), qty
    return body


class MtgoTradeBotClient:
    """Thin async wrapper over the serve API. All methods return the parsed JSON dict on success,
    or ``None`` on any failure / when disabled (they never raise to the caller)."""

    def __init__(self, url: Optional[str] = None, token: Optional[str] = None, timeout: float = 20.0,
                 *, url_env: str = "MTGO_TRADEBOT_URL", token_env: str = "MTGO_TRADEBOT_TOKEN"):
        # Which env vars to read is a parameter, not a constant, because there is
        # more than one MTGO account: the wallet's custodian and the lending
        # library. Passing url="" for an unconfigured client would otherwise fall
        # through this `or` chain to the OTHER account's credentials -- a library
        # with no config would quietly start trading out of the wallet.
        self.url = (url or os.getenv(url_env) or "").rstrip("/")
        self.token = token or os.getenv(token_env) or ""
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
                    mark_ambiguous: bool = False, mark_missing: bool = False):
        """One HTTP call. Returns the parsed body, or None on any DEFINITE failure
        (disabled, HTTP error status, connection never established).

        ``mark_ambiguous``: for POSTs whose delivery matters (they create serve jobs) —
        a timeout/reset AFTER the connection was made may mean the request landed and
        only the response was lost, so those return ``{"_ambiguous": True}`` instead of
        None, letting the caller run job-adoption recovery only when it's warranted.

        ``mark_missing``: 404 is the serve stating a FACT — it does not have this —
        where a 500 or a dropped read is it failing to answer, and the two want
        opposite handling. Opt-in so callers written against "None means anything
        went wrong" keep that contract."""
        if not self.enabled:
            logger.warning("MtgoTradeBotClient disabled (MTGO_TRADEBOT_URL / MTGO_TRADEBOT_TOKEN not set)")
            return None
        headers = {"Authorization": f"Bearer {self.token}"}
        full = f"{self.url}{path}"
        try:
            session = self._get_session()
            async with session.request(method, full, headers=headers, json=json, params=params) as resp:
                text = await resp.text()
                if resp.status == 404 and mark_missing:
                    return {"_missing": True}
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

    async def _list_jobs(self) -> Optional[list]:
        """The serve's job listing, newest first, or None if it can't be read."""
        listing = await self._call("GET", "/jobs")
        return None if not listing else listing.get("jobs", [])

    async def active_jobs(self) -> Optional[list]:
        """Jobs the serve is actually working (queued or running). NOT derived from
        /health's ``jobs`` field — that is a lifetime count including terminal jobs, so
        it stays >0 forever after the first trade."""
        jobs = await self._list_jobs()
        return None if jobs is None else [
            j for j in jobs if (j.get("state") or "").lower() in ("queued", "running")]

    # Which side of a job holds what the BOT is moving. A deposit and a return
    # both bring cards/tix in, so they are matched on `receive`; everything else
    # the bot hands out, so it is matched on `give`. Getting this backwards does
    # not error -- it silently never adopts anything.
    _JOB_ITEMS = {"deposit": "receive", "return": "receive"}

    async def _find_recent(self, job_type: str, mtgo_user: str, matches,
                           max_age_s: float):
        """Newest non-failed job of this type and user, created within
        ``max_age_s``, whose items satisfy ``matches``. Or None.

        Recovery for a POST whose response was lost: a request that reached the
        serve created a real job even though we never saw the 202, and adopting
        it keeps the ledger attached to a trade that may still complete.
        """
        jobs = await self._list_jobs()
        if not jobs:
            return None
        now = datetime.now(timezone.utc)
        for job in jobs:  # serve lists newest first
            if job.get("type") != job_type or job.get("state") == "failed":
                continue
            if (job.get("user") or "").lower() != mtgo_user.lower():
                continue
            if not matches(job.get(self._JOB_ITEMS.get(job_type, "give")) or []):
                continue
            try:
                ts = datetime.fromisoformat(job.get("createdAt") or "")
                if now - ts > timedelta(seconds=max_age_s):
                    continue
            except ValueError:
                pass  # unparseable timestamp: still adopt (better than stranding a live trade)
            return job
        return None

    async def find_recent_job(self, job_type: str, mtgo_user: str, qty: int,
                              max_age_s: float = 120.0):
        """Adopt a lost TIX job -- ``qty`` event tickets moving one way."""
        def matches(items):
            return bool(items) and (items[0].get("name") == EVENT_TICKET
                                    and items[0].get("qty") == qty)
        return await self._find_recent(job_type, mtgo_user, matches, max_age_s)

    async def find_recent_deck_job(self, job_type: str, mtgo_user: str, cards,
                                   max_age_s: float = 120.0):
        """Adopt a lost DECK job -- a whole card list moving one way.

        Matched on the complete list rather than its first entry: adopting a job
        that moves different cards would attach the loan to a trade the borrower
        never agreed to, and settlement records what the job carried as what
        they owe back.
        """
        want = sorted((c["name"], c["qty"]) for c in cards)

        def matches(items):
            return sorted((i.get("name"), i.get("qty")) for i in items) == want
        return await self._find_recent(job_type, mtgo_user, matches, max_age_s)

    async def get_job(self, job_id: str, *, mark_missing: bool = False):
        """One job's projection incl. its terminal ``state`` (queued|running|done|failed) + ``detail``.

        With ``mark_missing``, a job the serve has never heard of comes back as
        ``{"_missing": True}`` rather than None — the difference between "this
        trade is gone and can never report" and "I could not reach the serve",
        which is the difference between rolling a loan back and leaving it be.
        """
        return await self._call("GET", f"/jobs/{job_id}", mark_missing=mark_missing)

    # ---- jobs (each returns the 202 job dict, whose ``id`` you then poll) ----
    async def deposit(self, user: str, cards, qty: int = 1, commit: bool = True, wait_minutes: int = 0):
        """Bot RECEIVES cards FROM the user (a deposit into custody).

        ``cards`` takes the three shapes _order_body describes, so handing in a deck is
        ONE job and therefore one MTGO trade — the serve works a single job at a time, so
        a card-per-job deposit would be a sequence of separate trades, each with its own
        invite and handshake.
        """
        if not cards:
            logger.warning("Refusing a deposit with no cards (user={})", user)
            return None
        return await self._call("POST", "/deposit",
                                json=_order_body(user, cards, qty, commit, wait_minutes),
                                mark_ambiguous=True)

    async def give(self, user: str, card: str, qty: int = 1, commit: bool = True, wait_minutes: int = 0):
        """Bot GIVES qty of a card TO the user (a withdrawal, or a lend)."""
        return await self._call("POST", "/request", json={
            "user": user, "cards": [card], "qty": qty, "commit": commit, "waitMinutes": wait_minutes},
            mark_ambiguous=True)

    # ---- house card lending -------------------------------------------------------
    # These differ from give()/deposit() in one way that matters: the serve records WHICH
    # PRINTING crossed, and borrow/return/withdraw are the endpoints that read it back. We
    # send card NAMES and never a catId — the printing is the serve's to remember, which is
    # exactly why a caller here cannot get it wrong.

    async def borrow(self, user: str, cards, qty: int = 1, commit: bool = True,
                     wait_minutes: int = 0):
        """Bot LENDS cards to the user, expecting them back. The serve picks printings it
        owns and records them, so return() can ask for those exact copies.

        ``cards`` takes the same three shapes as deposit() -- a name, a list of names at
        ``qty`` each, or ``[{"name", "qty"}]`` per-card amounts. The third is what makes a
        whole DECK one order: the serve works one job at a time, so a card-per-job loan
        would be a sequence of separate trades, each with its own invite and handshake.
        """
        if not cards:
            # A loan that moves nothing is a bug in the caller, not an MTGO
            # failure; a job record for it would read as though the serve refused.
            logger.warning("Refusing a loan with no cards (user={})", user)
            return None
        return await self._call("POST", "/borrow",
                                json=_order_body(user, cards, qty, commit, wait_minutes),
                                mark_ambiguous=True)

    async def return_cards(self, user: str, card: str | None = None, qty: int | None = None,
                           commit: bool = True, wait_minutes: int = 0):
        """User RETURNS previously borrowed cards. The serve pins the exact printings it lent
        from its own movement record. Omit `card` to settle everything they hold of ours."""
        return await self._call("POST", "/return", json=_settle_body(user, card, qty, commit, wait_minutes),
                                mark_ambiguous=True)

    async def withdraw_cards(self, user: str, card: str | None = None, qty: int | None = None,
                             commit: bool = True, wait_minutes: int = 0):
        """Give back cards the user DEPOSITED, as the exact printings they handed over."""
        return await self._call("POST", "/withdraw", json=_settle_body(user, card, qty, commit, wait_minutes),
                                mark_ambiguous=True)

    async def positions(self, user: str | None = None):
        """What is on the far side of the boundary. With a user: {held[], lent[]}, each entry
        {card, catId, qty, since}. Without: {users[]}. Read-only."""
        return await self._call("GET", "/positions", params={"user": user} if user else None)

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


# The lending library is a SECOND MTGO account behind a second serve: the wallet's
# custodian holds tix, the library holds the cards players borrow. Separate
# accounts mean separate bearer tokens, so they cannot share one client -- and
# they must not fall back to each other. A library that quietly used the wallet's
# credentials would hand out the tix it is holding in escrow.
_lending_client: Optional[MtgoTradeBotClient] = None


def get_lending_client() -> MtgoTradeBotClient:
    """The client for the card-lending library's MTGO account.

    Disabled (every call returns None) unless MTGO_LENDING_URL and
    MTGO_LENDING_TOKEN are both set, mirroring get_client's guard.
    """
    global _lending_client
    if _lending_client is None:
        _lending_client = MtgoTradeBotClient(
            url_env="MTGO_LENDING_URL", token_env="MTGO_LENDING_TOKEN")
    return _lending_client
