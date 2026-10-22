"""Generate the scheduler contract's example files from the mockup's data.

Writes into the directory given as argv[1], else next to this script. Every file uses the same
encoding rules as the contract: 336 half-hour cells, MSB-first bitmaps, base64.
"""
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
OUT.mkdir(parents=True, exist_ok=True)
NY = ZoneInfo("America/New_York")
CELLS, MIN = 336, 30
DAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def b64(bits):
    raw = bytearray(CELLS // 8)
    for i in bits:
        raw[i // 8] |= 0x80 >> (i % 8)
    return base64.b64encode(bytes(raw)).decode()


def local_cells(*specs):
    """'Tue-Fri 19-23' -> usual-week cell indexes (Mon 00:00 local = 0), end hour exclusive."""
    out = set()
    for sp in specs:
        dd, hh = sp.split()
        d1, _, d2 = dd.partition("-")
        h1, h2 = map(int, hh.split("-"))
        for d in range(DAYS[d1], DAYS[d2 or d1] + 1):
            out.update(range(d * 48 + h1 * 2, d * 48 + h2 * 2))
    return out


# The round's window: 7 days from Mon 19 Oct 2026, 00:00 New York time.
START_LOCAL = datetime(2026, 10, 19, tzinfo=NY)
START_UTC = START_LOCAL.astimezone(timezone.utc)


def window_cells(zone, local_bits, start_utc):
    """THE conversion rule: window cell j is free when the local time of its start
    instant, in the player's zone, falls in a painted usual-week cell. A repeated
    hour (fall back) is free twice; a skipped hour (spring forward) never occurs."""
    z = ZoneInfo(zone)
    out = set()
    for j in range(CELLS):
        lt = (start_utc + timedelta(minutes=MIN * j)).astimezone(z)
        if lt.weekday() * 48 + (lt.hour * 60 + lt.minute) // 30 in local_bits:
            out.add(j)
    return out


def to_window(local_bits):
    return window_cells("America/New_York", local_bits, START_UTC)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cell_time(i):
    return START_UTC + timedelta(minutes=MIN * i)


WEEKS = {
    "good": {
        "Ali": ("Tue-Fri 19-23",), "Ben": ("Mon-Thu 18-23", "Sat 18-24"),
        "Cy": ("Wed-Sat 20-24",), "Dee": ("Tue 19-22", "Thu 19-22"),
        "Eve": ("Tue-Fri 19-24",), "Fay": ("Mon-Wed 18-22", "Fri 20-24"), "Gus": ("Thu-Sun 19-24",),
    },
    "poor": {
        "Ali": ("Mon-Fri 18-21",), "Ben": ("Mon-Thu 18-21",), "Cy": ("Tue-Thu 18-20",),
        "Dee": ("Mon 18-21", "Wed 18-21"),
        "Eve": ("Mon-Fri 22-24",), "Fay": ("Tue-Thu 20-24",), "Gus": ("Sat-Sun 18-24",),
    },
}
IDS = {n: str(900000000000000000 + k) for k, n in enumerate(["Ali", "Ben", "Cy", "Dee", "Eve", "Fay", "Gus"], 1)}
TEAMS = [("Gypsy Caravan", 41, ["Ali", "Ben", "Cy", "Dee"]), ("Elm Street", 42, ["Eve", "Fay", "Gus"])]


def holds_for(scen):
    """(label, kind, local start 'Day HH:MM', hours)"""
    base = [("Pine Barrens vs Lotus Eaters", "copy", "Tue 20", 4),
            ("Moxen Club vs Night Shift", "copy", "Thu 19", 4),
            ("Gypsy Caravan vs Elm Street", "copy", "Thu 21", 4)]
    sittings = [("Ben vs Fay (Ben's deck)", "deck", "Wed 19", 1.5, IDS["Ben"]),
                ("Ben vs Eve (Ben's deck)", "deck", "Tue 19", 1.5, IDS["Ben"])]
    if scen == "good":
        return base + sittings
    return base + sittings + [("Ash & Oak vs Crown Court", "copy", "Wed 20", 4),
                   ("Pine Barrens vs Night Shift", "copy", "Wed 19", 4),
                   ("Moxen Club vs Lotus Eaters", "copy", "Wed 20", 3),
                   ("Ash & Oak vs Crown Court", "copy", "Thu 20", 4)]


def window_index(day, hour):
    return to_window({DAYS[day] * 48 + hour * 2}).pop()


def build(scen):
    weeks = {n: to_window(local_cells(*s)) for n, s in WEEKS[scen].items()}
    holds = []
    used = [0.0] * CELLS
    # copies_free = whole league copies that could still be assembled = min over the
    # cube's cards of that card's free count. In these examples no two held decks
    # share a card, so any deck holds in a cell cost exactly one whole copy.
    copy_n = [0] * CELLS
    decks = [[] for _ in range(CELLS)]
    for item in holds_for(scen):
        label, kind, start, hrs = item[:4]
        owner = item[4] if len(item) > 4 else None
        d, h = start.split()
        i0 = window_index(d, int(h))
        i1 = i0 + int(hrs * 2)
        holds.append({"label": label, "kind": kind, "user": owner,
                      "start_utc": iso(cell_time(i0)), "end_utc": iso(cell_time(i1))})
        for i in range(i0, min(i1, CELLS)):
            if kind == "copy":
                copy_n[i] += 1
            else:
                decks[i].append(owner)
    copies_free = [max(0, 3 - copy_n[i] - (1 if decks[i] else 0)) for i in range(CELLS)]
    # Ben's own deck holds don't block his own new sitting (the cards are his).
    free_wo_ben = [max(0, 3 - copy_n[i] - (1 if [u for u in decks[i] if u != IDS["Ben"]] else 0))
                   for i in range(CELLS)]

    shared = {
        "format": "draftbot.scheduler.match", "version": 1, "seq": 17,
        "generated_at": "2026-10-19T14:02:11Z",
        "match": {"id": "4-3-7", "tournament_id": "4", "tournament_name": "Lotus Open Season 2", "round": 3,
                  "status": "open", "thread_url": "https://discord.com/channels/1355718878298116096/1563000000000000000"},
        "window": {"start_utc": iso(START_UTC), "cell_minutes": MIN, "cells": CELLS,
                   "display_zone": "America/New_York"},
        "rules": {"team_size": 3, "min_own_players": 2, "max_subs": 2,
                  "draft_cells": 2, "copy_offset_cells": 2, "copy_cells": 8, "sitting_cells": 3},
        "teams": [{"id": str(tid), "name": name, "players": [
            {"user": IDS[n], "name": n, "captain": k == 0,
             "source": "match" if n in ("Ali", "Eve") else "usual",
             "saved_at": "2026-10-19T13:40:00Z" if n in ("Ali", "Eve") else None,
             "cells": b64(weeks[n])} for k, n in enumerate(members)]}
            for name, tid, members in TEAMS],
        "library": {"enabled": True, "copies_total": 3, "copies_free": copies_free, "holds": holds},
        "booking": {"draft": None, "sittings": [
            {"players": [IDS["Ben"], IDS["Fay"]], "start_utc": iso(cell_time(window_index("Wed", 19))),
             "cells": 3, "by": IDS["Fay"], "decks_reserved": [IDS["Ben"]]},
            {"players": [IDS["Ben"], IDS["Eve"]], "start_utc": iso(cell_time(window_index("Tue", 19))),
             "cells": 3, "by": IDS["Ben"], "decks_reserved": [IDS["Ben"]]}]},
    }
    return shared, weeks, copies_free, free_wo_ben


for scen in ("good", "poor"):
    shared, weeks, free, free_ben = build(scen)
    (OUT / f"match-shared.{scen}.json").write_text(json.dumps(shared, indent=2) + "\n")
    # Ben's personal file: his deck check, and what happened to his requests.
    deck_ok = {i for i in range(CELLS - 2) if all(free_ben[j] >= 1 for j in range(i, i + 3))}
    me = {
        "format": "draftbot.scheduler.match_me", "version": 1, "seq": 17,
        "generated_at": "2026-10-19T14:02:11Z",
        "match_id": "4-3-7", "user": IDS["Ben"], "name": "Ben",
        "plays_after_draft": False, "needs_library": True,
        "deck": {"assigned": True, "cards": 45, "free_starts": b64(deck_ok),
                 "short_reason": "all 3 league copies are held" if scen == "poor" else None},
        "requests": [{"request_id": "7f3c2a9e-4d1b-4b8e-9f0a-1c2d3e4f5a6b", "status": "booked",
                      "kind": "book_sitting", "at": "2026-10-19T13:55:02Z", "reason": None}],
    }
    (OUT / f"match-me.{scen}.json").write_text(json.dumps(me, indent=2) + "\n")

usual = {"format": "draftbot.scheduler.usual_week", "version": 1, "zone": "America/New_York",
         "cells": b64(local_cells(*WEEKS["good"]["Ben"])), "saved_at": "2026-10-18T22:14:09Z"}
(OUT / "usual-week.json").write_text(json.dumps(usual, indent=2) + "\n")

ben_good = build("good")[1]["Ben"]
match_week = {"format": "draftbot.scheduler.match_week", "version": 1, "match_id": "4-3-7",
              "window_start_utc": iso(START_UTC), "cells": b64(ben_good - {window_index("Mon", 18)}),
              "saved_at": "2026-10-19T13:40:00Z"}
(OUT / "match-week.json").write_text(json.dumps(match_week, indent=2) + "\n")

req_draft = {"format": "draftbot.scheduler.request", "version": 1,
             "request_id": "0b9d6f1e-3a2c-4e5f-8a7b-9c0d1e2f3a4b", "match_id": "4-3-7",
             "kind": "book_draft", "start_utc": iso(cell_time(window_index("Tue", 20))),
             "seen_seq": 17, "made_at": "2026-10-19T14:03:30Z"}
req_sit = {"format": "draftbot.scheduler.request", "version": 1,
           "request_id": "7f3c2a9e-4d1b-4b8e-9f0a-1c2d3e4f5a6b", "match_id": "4-3-7",
           "kind": "book_sitting", "opponent": IDS["Eve"],
           "start_utc": iso(cell_time(window_index("Tue", 19))), "reserve_deck": True,
           "seen_seq": 16, "made_at": "2026-10-19T13:55:00Z"}
(OUT / "request.book-draft.json").write_text(json.dumps(req_draft, indent=2) + "\n")
(OUT / "request.book-sitting.json").write_text(json.dumps(req_sit, indent=2) + "\n")

lfg = {"format": "draftbot.scheduler.lfg", "version": 1, "seq": 5, "generated_at": "2026-10-19T14:00:00Z",
       "guild": "1355718878298116096", "cards": [
           {"card_id": "12", "kind": "pair", "note": "Pair, Eastern, need a 3rd", "members": [
               {"user": "900000000000000011", "name": "Rook", "zone": "America/New_York",
                "cells": b64(local_cells("Mon-Thu 19-23"))},
               {"user": "900000000000000012", "name": "Wren", "zone": "America/New_York",
                "cells": b64(local_cells("Tue-Fri 19-24"))}]},
           {"card_id": "13", "kind": "solo", "note": "Solo, UK evenings", "members": [
               {"user": "900000000000000013", "name": "Thistle", "zone": "Europe/London",
                "cells": b64(local_cells("Sat-Sun 23-24", "Wed 2-4"))}]},
       ]}
(OUT / "lfg.json").write_text(json.dumps(lfg, indent=2) + "\n")

# Conversion cases: windows start at league (New York) midnight; the player's zone
# may differ. Across clock changes. Both the bot (Python) and the page (JS) must produce these bitmaps.
cases = []
for name, zone, spec, start in [
        ("new-york-fall-back", "America/New_York", ("Sun 0-3", "Sun 19-21", "Mon 19-21"), datetime(2026, 10, 26)),
        ("london-fall-back", "Europe/London", ("Sun 0-3", "Sat 20-22", "Sun 20-22"), datetime(2026, 10, 19)),
        ("new-york-spring-forward", "America/New_York", ("Sun 1-4", "Sun 19-21"), datetime(2027, 3, 8))]:
    bits = local_cells(*spec)
    start_utc = start.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    st, win = iso(start_utc), window_cells(zone, bits, start_utc)
    cases.append({"name": name, "zone": zone, "usual_cells": b64(bits),
                  "usual_readable": list(spec), "window_start_utc": st,
                  "expected_window_cells": b64(win),
                  "expected_free_utc": sorted(iso(datetime.fromisoformat(st.replace("Z", "+00:00")) + timedelta(minutes=30 * j)) for j in win)})
(OUT / "conversion.json").write_text(json.dumps({"format": "draftbot.scheduler.test_conversion", "version": 1, "cases": cases}, indent=2) + "\n")
print("\n".join(sorted(p.name for p in OUT.iterdir())))
