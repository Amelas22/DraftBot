"""Does what the MTGO accounts physically hold match what the ledgers say they should?

Two audits, one per asset, each comparing the serve's ``GET /inventory`` -- the
complete per-printing listing of an account -- against the claim ledger that is
supposed to describe it:

  * Tix, on the wallet custodian: vault tix == SUM(wallet_tx). The invariant
    wallet_service has always stated; this adds the guard that makes a reading
    trustworthy, and names the trades that can explain a difference.
  * Cards, on the lending account: every copy the libraries own (custody owed
    to HOUSE_LIBRARY, across every library scope) minus the copies out with
    borrowers (owed to HOUSE_MTGO) is what should be sitting in the account.

Neither audit changes anything. A difference is reported for a human to settle;
the ledgers stay the record of who is owed what, and nothing here decides which
side is wrong.

What makes a reading count. A trade moves the vault before DraftBot books it, so
a difference seen while one is moving is the trade, not a discrepancy. The serve
says whether its snapshot is ``consistent`` (no trade running, none completed
since the scan), and DraftBot knows which of its own trades have not settled.
Only a consistent reading with nothing in flight is called a mismatch outright;
anything else is reported with the trades that may account for it.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger
from sqlalchemy import or_, select

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.debt_ledger import DebtLedger
from models.mtgo_job import MtgoJob
from services import debt_service, wallet_service
from services.card_deposit_service import JOB_KIND as CARD_DEPOSIT_KIND
from services.card_deposit_service import WITHDRAW_KIND as CARD_WITHDRAW_KIND
from services.card_lending_service import _PROMISED_STATES
from services.mtgo_tradebot_client import (
    Inventory, MtgoTradeBotClient, card_key, get_client, get_lending_client,
)
from services.serve_health_monitor import ALERT_ENV

# Job statuses that are finished; anything else may still move the vault.
_SETTLED_JOB_STATUSES = ("done", "failed")
_TIX_JOB_KINDS = ("deposit", "withdraw")
_CARD_JOB_KINDS = ("borrow", "return", CARD_DEPOSIT_KIND, CARD_WITHDRAW_KIND)

# Loan states with a trade that may be moving cards right now: the ones lending
# already treats as promised (offered, or unaccounted for), plus on the way back.
_MOVING_LOAN_STATES = _PROMISED_STATES + ("return_pending",)

# Verdicts, in the order an operator should worry about them.
UNREADABLE = "unreadable"   # no inventory to compare against
MISMATCH = "mismatch"       # a consistent reading, nothing in flight, and a difference
UNSETTLED = "unsettled"     # a difference, but a trade is moving or the reading is stale
CLEAN = "clean"


@dataclass(frozen=True)
class CardLine:
    """One card whose physical count differs from the ledger's."""
    name: str
    expected: int
    held: int

    @property
    def diff(self) -> int:
        """Held minus expected: negative is missing, positive is extra."""
        return self.held - self.expected


@dataclass(frozen=True)
class InFlight:
    """A trade DraftBot has not finished booking, which may explain a difference."""
    what: str      # "job" or "loan"
    ref: str       # job id, or loan id
    kind: str      # job kind, or loan state
    detail: str


@dataclass
class TixAudit:
    verdict: str
    reason: str = ""
    bot_tix: Optional[int] = None
    wallet_total: Optional[int] = None
    consistent: bool = False
    in_flight: "list[InFlight]" = field(default_factory=list)

    @property
    def diff(self) -> Optional[int]:
        if self.bot_tix is None or self.wallet_total is None:
            return None
        return self.bot_tix - self.wallet_total


@dataclass
class CardAudit:
    verdict: str
    reason: str = ""
    consistent: bool = False
    expected_total: int = 0
    held_total: int = 0
    missing: "list[CardLine]" = field(default_factory=list)
    extra: "list[CardLine]" = field(default_factory=list)
    in_flight: "list[InFlight]" = field(default_factory=list)


def compare(expected: "dict[str, int]", held: "dict[str, int]"
            ) -> "tuple[list[CardLine], list[CardLine]]":
    """(missing, extra): every card whose held count differs from expected.

    Names are compared by card_key, so "Lightning Bolt" and "lightning bolt " are
    one card. The ledger's spelling is reported where it has one. Missing is
    sorted largest shortfall first, which is the order to chase them in.
    """
    exp, have = _fold(expected), _fold(held)

    missing: "list[CardLine]" = []
    extra: "list[CardLine]" = []
    for key in exp.keys() | have.keys():
        name = exp[key][0] if key in exp else have[key][0]
        line = CardLine(name, exp.get(key, ("", 0))[1], have.get(key, ("", 0))[1])
        if line.diff < 0:
            missing.append(line)
        elif line.diff > 0:
            extra.append(line)
    missing.sort(key=lambda c: (c.diff, c.name.lower()))
    extra.sort(key=lambda c: (-c.diff, c.name.lower()))
    return missing, extra


def _fold(counts: "dict[str, int]") -> "dict[str, tuple[str, int]]":
    """{card_key: (first spelling seen, total copies)}."""
    out: "dict[str, tuple[str, int]]" = {}
    for name, qty in counts.items():
        key = card_key(name)
        spelled, total = out.get(key, (name, 0))
        out[key] = (spelled, total + qty)
    return out


def verdict_for(differs: bool, consistent: bool, in_flight: "list[InFlight]") -> str:
    """The one place a reading is judged.

    An inconsistent reading is evidence of nothing in either direction: a
    snapshot from before cards left can agree with a ledger that no longer
    matches the shelf. Only a consistent one can be CLEAN or a MISMATCH, and a
    difference with our own trades still unbooked is UNSETTLED -- the trade may
    be the difference.
    """
    if not consistent:
        return UNSETTLED
    if not differs:
        return CLEAN
    return UNSETTLED if in_flight else MISMATCH


# ---------------------------------------------------------------------------
# what the ledger says
# ---------------------------------------------------------------------------
async def _library_scopes() -> "list[str]":
    """Every custody scope holding card rows: each library's, plus the flat
    pre-library "library" scope. All of them share the one lending account."""
    prefix = wallet_service.LIBRARY_SCOPE
    async with AsyncSessionLocal() as session:
        rows = (await session.scalars(
            select(DebtLedger.guild_id).where(
                DebtLedger.card_name.isnot(None),
                or_(DebtLedger.guild_id == prefix,
                    DebtLedger.guild_id.like(f"{prefix}:%")),
            ).distinct())).all()
    return sorted(str(r) for r in rows)


async def expected_library_stock() -> "dict[str, int]":
    """What the lending account should hold right now: {card: copies}.

    Owned (custody the house owes its donors) minus out (copies borrowers owe the
    house back). A card fully out on loan nets to zero and is left out.
    """
    stock: "dict[str, int]" = {}
    for scope in await _library_scopes():
        owned = await debt_service.get_cards_owed_by(scope, wallet_service.HOUSE_LIBRARY)
        for name, qty in owned.items():
            stock[name] = stock.get(name, 0) + qty
        out = await debt_service._open_card_groups(
            scope, counterparty_id=wallet_service.HOUSE_MTGO)
        for group in out.values():
            if group["net"] < 0:   # the borrower's side: they owe the house copies
                name = group["card_name"]
                stock[name] = stock.get(name, 0) + group["net"]
    return {name: qty for name, qty in stock.items() if qty != 0}


async def _jobs_in_flight(kinds: "tuple[str, ...]") -> "list[InFlight]":
    async with AsyncSessionLocal() as session:
        jobs = (await session.scalars(
            select(MtgoJob).where(
                MtgoJob.kind.in_(kinds),
                MtgoJob.status.notin_(_SETTLED_JOB_STATUSES),
            ).order_by(MtgoJob.created_at))).all()
    return [InFlight("job", str(j.job_id), str(j.kind),
                     f"{j.status}: {j.amount} {j.card_name or ('tix' if j.kind in _TIX_JOB_KINDS else 'cards')}"
                     f" for {j.mtgo_user}")
            for j in jobs]


async def _loans_in_flight() -> "list[InFlight]":
    async with AsyncSessionLocal() as session:
        loans = (await session.scalars(
            select(CardLoan).where(CardLoan.state.in_(_MOVING_LOAN_STATES))
            .order_by(CardLoan.id))).all()
    return [InFlight("loan", str(loan.id), str(loan.state),
                     f"{sum(int(c.get('qty') or 0) for c in loan.offered_cards)} cards, "
                     f"borrower {loan.borrower_id}")
            for loan in loans]


# ---------------------------------------------------------------------------
# the audits
# ---------------------------------------------------------------------------
async def _read(client: MtgoTradeBotClient) -> "tuple[Optional[Inventory], str]":
    if not client.enabled:
        return None, "the serve is not configured"
    inventory = await client.inventory(refresh=True)
    if inventory is None:
        return None, ("could not read /inventory -- the serve is unreachable, has not "
                      "finished a scan, or predates the route")
    return inventory, ""


async def audit_tix(client: Optional[MtgoTradeBotClient] = None) -> TixAudit:
    """Vault tix on the custodian vs. the sum of every wallet."""
    inventory, why = await _read(client or get_client())
    if inventory is None:
        return TixAudit(UNREADABLE, reason=why)
    wallet_total = await wallet_service.total_wallets()
    in_flight = await _jobs_in_flight(_TIX_JOB_KINDS)
    verdict = verdict_for(inventory.tix != wallet_total, inventory.consistent, in_flight)
    if verdict == MISMATCH:
        logger.warning(f"TIX AUDIT MISMATCH: vault={inventory.tix} wallets={wallet_total} "
                       f"diff={inventory.tix - wallet_total:+d}")
    return TixAudit(verdict, bot_tix=inventory.tix, wallet_total=wallet_total,
                    consistent=inventory.consistent, in_flight=in_flight)


async def audit_library_cards(client: Optional[MtgoTradeBotClient] = None) -> CardAudit:
    """The lending account's cards vs. what the libraries' ledger says it holds."""
    # The ledger read does not depend on the scan, so it runs behind it.
    (inventory, why), expected = await asyncio.gather(
        _read(client or get_lending_client()), expected_library_stock())
    if inventory is None:
        return CardAudit(UNREADABLE, reason=why)
    held = inventory.by_name(key=str)
    missing, extra = compare(expected, held)
    in_flight = await _jobs_in_flight(_CARD_JOB_KINDS) + await _loans_in_flight()
    # Extra copies are reported but are not what makes a mismatch: the account
    # may hold cards nobody booked to a library (seeded stock, its own cards).
    # Missing copies are somebody's cards that are not there.
    verdict = verdict_for(bool(missing), inventory.consistent, in_flight)
    if verdict == MISMATCH:
        logger.warning("CARD AUDIT MISMATCH: missing " + ", ".join(
            f"{-m.diff}x {m.name}" for m in missing[:20]))
    return CardAudit(verdict, consistent=inventory.consistent,
                     expected_total=sum(q for q in expected.values() if q > 0),
                     held_total=sum(held.values()),
                     missing=missing, extra=extra, in_flight=in_flight)



# ---------------------------------------------------------------------------
# describing a result -- shared by the admin command and the alert DM
# ---------------------------------------------------------------------------
def describe_tix(audit: TixAudit) -> str:
    if audit.verdict == UNREADABLE:
        return f"Tix: couldn't audit -- {audit.reason}"
    head = (f"Tix: vault **{audit.bot_tix}**, wallets **{audit.wallet_total}**, "
            f"difference **{audit.diff:+d}**")
    return head + _tail(audit.verdict, audit.consistent, audit.in_flight)


def describe_cards(audit: CardAudit, limit: int = 15) -> str:
    if audit.verdict == UNREADABLE:
        return f"Library cards: couldn't audit -- {audit.reason}"
    lines = [f"Library cards: ledger expects **{audit.expected_total}** on the shelf, "
             f"the account holds **{audit.held_total}**"]
    if audit.missing:
        lines.append(f"Missing ({sum(-m.diff for m in audit.missing)} copies of "
                     f"{len(audit.missing)} cards):")
        lines += [f"- {-m.diff}x {m.name} (ledger {m.expected}, held {m.held})"
                  for m in audit.missing[:limit]]
        if len(audit.missing) > limit:
            lines.append(f"- ...and {len(audit.missing) - limit} more")
    if audit.extra:
        lines.append(f"Not on any library's books: {sum(e.diff for e in audit.extra)} "
                     f"copies of {len(audit.extra)} cards (informational)")
    return "\n".join(lines) + _tail(audit.verdict, audit.consistent, audit.in_flight)


def _tail(verdict: str, consistent: bool, in_flight: "list[InFlight]", limit: int = 10) -> str:
    if verdict == CLEAN:
        return "\nBalanced."
    out = []
    if verdict == MISMATCH:
        out.append("MISMATCH: a settled reading with no trade in flight, and they disagree.")
    else:
        if not consistent:
            out.append("A trade was running or finished during the read, so the "
                       "difference may be that trade. Run it again in a minute.")
        if in_flight:
            out.append(f"{len(in_flight)} trade(s) not yet booked, which may explain it:")
            out += [f"- {f.what} {f.ref} ({f.kind}) {f.detail}" for f in in_flight[:limit]]
            if len(in_flight) > limit:
                out.append(f"- ...and {len(in_flight) - limit} more")
    return "\n" + "\n".join(out)


# ---------------------------------------------------------------------------
# the drift watchdog
# ---------------------------------------------------------------------------
AUDIT_INTERVAL_S = 30 * 60


class DriftAlerter:
    """Decides when a mismatch is worth a DM.

    Only a MISMATCH counts, and only the same one twice running: a single
    reading can catch a trade the serve finished a moment ago, and the second
    look, half an hour later, is what says it did not settle. Told once per
    distinct mismatch, and once more when it clears, so a standing problem is
    not repeated every half hour and its resolution is not silent.
    """

    def __init__(self) -> None:
        self._last: Optional[tuple[Any, ...]] = None
        self._alerted: Optional[tuple[Any, ...]] = None

    def observe(self, verdict: str, fingerprint: "tuple[Any, ...]") -> Optional[str]:
        """'alert', 'resolved', or None. Call confirm() once the DM is sent."""
        if verdict == UNREADABLE or verdict == UNSETTLED:
            return None   # no evidence either way; keep what we knew
        if verdict == CLEAN:
            self._last = None
            return "resolved" if self._alerted is not None else None
        seen_before = self._last == fingerprint
        self._last = fingerprint
        if seen_before and self._alerted != fingerprint:
            return "alert"
        return None

    def confirm(self, said: str, fingerprint: "tuple[Any, ...]") -> None:
        self._alerted = fingerprint if said == "alert" else None


def tix_fingerprint(audit: TixAudit) -> "tuple[Any, ...]":
    return ("tix", audit.diff)


def cards_fingerprint(audit: CardAudit) -> "tuple[Any, ...]":
    return ("cards",) + tuple((card_key(m.name), m.diff) for m in audit.missing)


async def inventory_audit_watchdog(bot: Any) -> None:
    """Audit both accounts every AUDIT_INTERVAL_S and DM the serve alert
    recipient (SERVE_ALERT_DISCORD_ID) about a mismatch that persists."""
    import os
    from notification_service import send_dm

    alert_to = os.getenv(ALERT_ENV, "").strip()
    if not alert_to:
        logger.warning(f"inventory audit: {ALERT_ENV} not set -- audits will only be logged")
    tix_alerts, card_alerts = DriftAlerter(), DriftAlerter()

    async def check(alerter: DriftAlerter, verdict: str, fp: "tuple[Any, ...]",
                    text: str) -> None:
        said = alerter.observe(verdict, fp)
        if said is None:
            return
        message = (f"⚠️ **Inventory audit mismatch** (seen twice, 30 min apart)\n{text}"
                   if said == "alert" else f"✅ **Inventory audit back in balance**\n{text}")
        logger.info(f"inventory audit: {said} -- {text.splitlines()[0]}")
        if alert_to and await send_dm(bot, alert_to, message[:1900], label="inventory audit"):
            alerter.confirm(said, fp)

    while True:
        try:
            # Two accounts, two serves: scanned side by side, and one failing must
            # not silence the other's alert. An unconfigured serve comes back
            # UNREADABLE, which the alerter ignores.
            tix, cards = await asyncio.gather(audit_tix(), audit_library_cards(),
                                              return_exceptions=True)
            if isinstance(tix, TixAudit):
                await check(tix_alerts, tix.verdict, tix_fingerprint(tix), describe_tix(tix))
            else:
                logger.opt(exception=tix).error("inventory audit: tix audit failed")
            if isinstance(cards, CardAudit):
                await check(card_alerts, cards.verdict, cards_fingerprint(cards),
                            describe_cards(cards))
            else:
                logger.opt(exception=cards).error("inventory audit: card audit failed")
        except Exception:
            logger.opt(exception=True).error("inventory audit: run failed")
        await asyncio.sleep(AUDIT_INTERVAL_S)
