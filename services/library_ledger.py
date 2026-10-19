"""Pure rules of the card-library ledger: copies owned, peak holds, capacity.

No database and no network, so the capacity maths can be tested exhaustively and
reused by the store, the reservation path and the status text alike.

A "copy" is enough of every card in a cube to draft it once. One copy is set
aside for server play; league holds ("match", "sitting") share the rest, while
server holds may use any copy.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

SERVER_WINDOW = timedelta(hours=4)
SERVER_SET_ASIDE = 1
_LEAGUE_KINDS = {"match", "sitting"}


@dataclass(frozen=True)
class Span:
    """One hold on a copy, from ``starts_at`` until released (or at least ``ends_at``)."""

    kind: str
    starts_at: datetime
    ends_at: datetime
    released_at: datetime | None


@dataclass(frozen=True)
class Verdict:
    """Whether one more hold fits, with the numbers behind the answer for logs."""

    ok: bool
    owned: int
    peak: int
    limit: int
    reason: str


def copies_owned(holdings: dict[str, int], cube_cards: list[dict[str, Any]]) -> int:
    """Copies of the cube the library can field: the scarcest card sets the count."""
    per_card = [
        holdings.get(card["name"], 0) // card["qty"]
        for card in cube_cards
        if card["qty"] > 0
    ]
    return min(per_card, default=0)


def counts_until(span: Span, now: datetime) -> datetime:
    """End of the span's effective interval.

    An unreleased hold keeps counting past ``ends_at`` because the deck is still
    live until someone releases it.
    """
    if span.released_at is not None:
        return span.released_at
    return max(span.ends_at, now)


def peak_held(
    spans: list[Span],
    start: datetime,
    end: datetime,
    now: datetime,
    kinds: set[str] | None = None,
) -> int:
    """Most spans (of ``kinds``, if given) active at once anywhere in ``[start, end)``.

    Intervals are half-open, so a span ending exactly as another starts does not
    overlap it. Occupancy only rises at a span start, so checking ``start`` and
    every start inside the window finds the maximum.
    """
    live = [
        (s.starts_at, counts_until(s, now))
        for s in spans
        if kinds is None or s.kind in kinds
    ]
    live = [(a, b) for a, b in live if a < b]
    instants = {start} | {a for a, _ in live if start <= a < end}
    return max(
        (sum(1 for a, b in live if a <= t < b) for t in instants),
        default=0,
    )


def verdict(
    kind: str,
    owned: int,
    spans: list[Span],
    start: datetime,
    end: datetime,
    now: datetime,
) -> Verdict:
    """Decide whether one more hold of ``kind`` over ``[start, end)`` fits."""
    total = peak_held(spans, start, end, now)
    if kind not in _LEAGUE_KINDS:
        ok = total + 1 <= owned
        return Verdict(ok, owned, total, owned, "ok" if ok else "library full")

    ceiling = max(owned - SERVER_SET_ASIDE, 0)
    league = peak_held(spans, start, end, now, _LEAGUE_KINDS)
    if total + 1 > owned:
        return Verdict(False, owned, total, ceiling, "library full")
    if league + 1 > ceiling:
        return Verdict(False, owned, league, ceiling, "league stock full")
    return Verdict(True, owned, league, ceiling, "ok")
