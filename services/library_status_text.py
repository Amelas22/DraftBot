"""The text of /library status: copies owned, held and free, and the next day of holds.

Pure formatting over the ledger's spans, so the numbers can be checked without
a database or a Discord client.
"""
from datetime import datetime, timedelta

from services.library_ledger import LEAGUE_KINDS, SERVER_SET_ASIDE, Span, peak_held

DISCORD_LIMIT = 2000
_HOURS = 24


def status_lines(cube: str, owned: int, spans: list[Span], now: datetime) -> list[str]:
    """Header lines, then one line per hour for the next 24 hours.

    The result joins to at most 2,000 characters; if the hourly lines would
    overflow, they are cut and a final "…" line says so.
    """
    hour = timedelta(hours=1)
    held_now = peak_held(spans, now, now + timedelta(microseconds=1), now)
    league_now = peak_held(
        spans, now, now + timedelta(microseconds=1), now, kinds=LEAGUE_KINDS)
    league_total = max(owned - SERVER_SET_ASIDE, 0)
    # A league hold needs room in league stock and in the total.
    league_free = max(min(league_total - league_now, owned - held_now), 0)
    head = [
        f"{owned} copies of {cube} · {held_now} held now · {max(owned - held_now, 0)} free",
        f"League stock: {league_free} of {league_total} free · Server copy: always kept",
    ]
    hourly = []
    for i in range(_HOURS):
        start = now + i * hour
        peak = peak_held(spans, start, start + hour, now)
        hourly.append(f"<t:{int(start.timestamp())}:t> · {peak} held")

    lines = head + hourly
    if len("\n".join(lines)) <= DISCORD_LIMIT:
        return lines
    kept = list(head)
    for line in hourly:
        if len("\n".join(kept + [line, "…"])) > DISCORD_LIMIT:
            break
        kept.append(line)
    return kept + ["…"]
