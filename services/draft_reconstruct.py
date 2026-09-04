"""Rebuild every physical booster's journey around the table from a Draftmancer log.

A stored log holds each seat's picks in isolation: "here is the booster I saw,
here is what I took". Nobody records that seat A's leftovers became seat B's
pick 2 -- that has to be recovered by matching contents, and it is what turns
six personal logs into one shared table you can watch.

Two things make the recovery less obvious than it sounds:

* **Passing direction alternates per pack** (left, right, left). It is read
  off the log here rather than assumed, because a wrong guess still produces
  a plausible-looking chain for most boosters.
* **A booster can gain a card mid-flight.** Cogwork Librarian is exiled from a
  seat's pool to draft an extra card and goes back into that booster, so pack
  sizes do not simply count down and the same card can be drafted twice.
"""
from dataclasses import dataclass, field
from typing import Any

@dataclass
class Step:
    """One seat's turn with one booster."""
    pack: int                       # 1-indexed
    pick: int                       # 1-indexed
    seat: str
    contents: list[str]                  # card ids in the booster as this seat saw it
    taken: list[str]                     # card ids this seat drafted (2 on a Librarian pick)
    inserted: list[str]                  # card ids this seat added to the booster
    carddata: dict[str, Any] = field(repr=False, default_factory=dict)

    def card_name(self, card_id: str) -> str:
        return (self.carddata.get(card_id) or {}).get("name") or str(card_id)


@dataclass
class Booster:
    """One physical pack, followed from opening to exhaustion."""
    index: int                      # stable 1..18 id for the viewer
    pack: int
    opener: str
    steps: list[Step]


@dataclass
class Event:
    """Something the viewer should mark on the clock."""
    pack: int
    pick: int
    seat: str
    description: str


@dataclass
class Fate:
    """Where one card ended up, and how long it took to get there."""
    card_id: str
    name: str
    pack: int
    booster_index: int
    first_seen_pick: int
    taken_at_pick: int
    taken_by: str
    wheeled: bool
    passed_by: list[str]                 # seats that saw it and let it go


@dataclass
class Reconstruction:
    ring: list[str]
    directions: list[int]
    boosters: list[Booster]
    events: list[Event]
    carddata: dict[str, Any] = field(repr=False, default_factory=dict)

    def card_name(self, card_id: str) -> str:
        return (self.carddata.get(card_id) or {}).get("name") or str(card_id)


def _rows_by_seat(draft: dict[str, Any]) -> dict[str, dict[tuple[int, int], dict[str, Any]]]:
    """{seat name: {(pack, pick): pick row}}, both indices 0-based as stored."""
    rows: dict[str, dict[tuple[int, int], dict[str, Any]]] = {}
    for seat in draft["users"].values():
        name = str(seat.get("userName"))
        rows[name] = {(r["packNum"], r["pickNum"]): r for r in seat.get("picks") or []}
    return rows


def _passed_on(row: dict[str, Any]) -> set[str]:
    """The card ids a seat handed to the next seat."""
    gone = set(row.get("pick") or []) | set(row.get("burn") or [])
    return {card for i, card in enumerate(row["booster"]) if i not in gone}


def passing_ring(draft: dict[str, Any], start: str | None = None) -> list[str]:
    """Seat names in pack-1 passing order, beginning at `start`.

    Seat Y sits downstream of X when Y's pick-2 booster is exactly X's pick-1
    booster minus what X took.
    """
    rows = _rows_by_seat(draft)
    firsts = {name: r[(0, 0)] for name, r in rows.items() if (0, 0) in r}
    seconds = {name: set(r[(0, 1)]["booster"]) for name, r in rows.items() if (0, 1) in r}

    downstream: dict[str, str] = {}
    for name, row in firsts.items():
        handed = _passed_on(row)
        for other, booster in seconds.items():
            if other != name and booster == handed:
                downstream[name] = other
                break
    if len(downstream) != len(firsts):
        raise ValueError(
            f"could not resolve the passing ring: matched "
            f"{len(downstream)}/{len(firsts)} seats"
        )

    start = start or str(next(iter(draft["users"].values())).get("userName"))
    if start not in downstream:
        raise ValueError(f"no seat named {start!r} in this draft")
    ring: list[str] = []
    seat = start
    for _ in range(len(downstream)):
        ring.append(seat)
        seat = downstream[seat]
    return ring


def _links_matching(rows: dict[str, dict[tuple[int, int], dict[str, Any]]],
                     ring: list[str], pack: int, direction: int) -> int:
    """How many consecutive-holder links hold under one passing direction.

    Scored rather than required-perfect: a Cogwork Librarian insertion breaks
    one link in an otherwise correct direction, and the wrong direction breaks
    nearly all of them, so the maximum is unambiguous.
    """
    seats = len(ring)
    matches = 0
    for origin in range(seats):
        for pick in range(seats * 3):
            here = rows[ring[(origin + direction * pick) % seats]].get((pack, pick))
            nxt = rows[ring[(origin + direction * (pick + 1)) % seats]].get((pack, pick + 1))
            if here and nxt and _passed_on(here) == set(nxt["booster"]):
                matches += 1
    return matches


def pack_directions(draft: dict[str, Any], ring: list[str] | None = None) -> list[int]:
    """+1 (passing left) or -1 (right) for each pack, read from the log."""
    ring = ring or passing_ring(draft)
    rows = _rows_by_seat(draft)
    packs = sorted({p for r in rows.values() for p, _ in r})
    directions: list[int] = []

    for pack in packs:
        score_positive = _links_matching(rows, ring, pack, 1)
        score_negative = _links_matching(rows, ring, pack, -1)
        directions.append(1 if score_positive >= score_negative else -1)

    return directions


def reconstruct(draft: dict[str, Any]) -> Reconstruction:
    """Every booster in the draft, each followed seat by seat."""
    ring = passing_ring(draft)
    directions = pack_directions(draft, ring)
    rows = _rows_by_seat(draft)
    carddata: dict[str, Any] = draft.get("carddata") or {}
    seats = len(ring)

    boosters: list[Booster] = []
    events: list[Event] = []
    index = 0
    for pack_idx, direction in enumerate(directions):
        picks = sorted({p for r in rows.values() for pk, p in r if pk == pack_idx})
        for origin in range(seats):
            index += 1
            steps: list[Step] = []
            previous_passed: set[str] | None = None
            for pick in picks:
                seat = ring[(origin + direction * pick) % seats]
                row = rows[seat].get((pack_idx, pick))
                if row is None:
                    break
                contents = list(row["booster"])
                gone = set(row.get("pick") or [])
                taken = [contents[i] for i in sorted(gone) if i < len(contents)]
                inserted = ([] if previous_passed is None
                            else [c for c in contents if c not in previous_passed])
                steps.append(Step(pack_idx + 1, pick + 1, seat, contents, taken,
                                  inserted, carddata))
                if inserted:
                    names = ", ".join((carddata.get(c) or {}).get("name", c)
                                      for c in inserted)
                    events.append(Event(
                        pack_idx + 1, pick + 1, seat,
                        f"{seat} put {names} back into the pack to draft "
                        f"{len(taken)} cards at once",
                    ))
                previous_passed = set(contents) - set(taken)
            boosters.append(Booster(index, pack_idx + 1, ring[origin], steps))

    return Reconstruction(ring, directions, boosters, events, carddata)


def card_fates(result: Reconstruction) -> dict[str, Fate]:
    """{card name: Fate} -- where each distinct card finally went.

    A card drafted twice (put back into the pack, then taken again) keeps its
    last taker, so the index matches the finished pools.
    """
    seats = len(result.ring)
    fates: dict[str, Fate] = {}
    for booster in result.boosters:
        seen_at: dict[str, int] = {}
        passed_by: dict[str, list[str]] = {}
        for step in booster.steps:
            for card in step.contents:
                seen_at.setdefault(card, step.pick)
                passed_by.setdefault(card, [])
            for card in step.contents:
                if card not in step.taken:
                    passed_by[card].append(step.seat)
            for card in step.taken:
                first = seen_at[card]
                fates[result.card_name(card)] = Fate(
                    card_id=card,
                    name=result.card_name(card),
                    pack=step.pack,
                    booster_index=booster.index,
                    first_seen_pick=first,
                    taken_at_pick=step.pick,
                    taken_by=step.seat,
                    wheeled=(step.pick - first) >= seats,
                    passed_by=list(passed_by[card]),
                )
    return fates


def seat_pools(result: Reconstruction) -> dict[str, list[dict[str, Any]]]:
    """{seat: [{card, pack, pick, gone}, ...]} -- every card a seat drafted, in order.

    `gone` is the (pack, pick) at which the card left the pool again, or None.
    Cogwork Librarian is drafted like anything else and then spent, so a pool
    that only ever grows would keep showing it in the seat's pile for the rest
    of the draft and double-count it against whoever drafted it next.
    """
    pools: dict[str, list[dict[str, Any]]] = {seat: [] for seat in result.ring}
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    all_steps: list[Step] = [s for b in result.boosters for s in b.steps]
    def step_key(s: Step) -> tuple[int, int]:
        return (s.pack, s.pick)
    sorted_steps = sorted(all_steps, key=step_key)
    for step in sorted_steps:
        for card in step.taken:
            entry: dict[str, Any] = {"card": card, "pack": step.pack, "pick": step.pick, "gone": None}
            pools[step.seat].append(entry)
            entries[(step.seat, card)] = entry
        for card in step.inserted:
            # The seat spends a card out of its own pool to put it back in the
            # booster, so close the entry it was drafted under.
            spent = entries.get((step.seat, card))
            if spent is not None:
                spent["gone"] = [step.pack, step.pick]
    return pools


def pool_through(result: Reconstruction, seat: str, pack: int, pick: int,
                  pools: dict[str, list[dict[str, Any]]] | None = None) -> list[str]:
    """Card ids `seat` is holding once (pack, pick) has been played."""
    pools = pools if pools is not None else seat_pools(result)
    held: list[str] = []
    for entry in pools[seat]:
        if (entry["pack"], entry["pick"]) > (pack, pick):
            break
        if entry["gone"] and tuple(entry["gone"]) <= (pack, pick):
            continue
        held.append(entry["card"])
    return held


def taken_in(result: Reconstruction) -> dict[int, dict[str, dict[str, Any]]]:
    """{booster index: {card id: {"pick", "seat"}}} -- who took each card *there*.

    Scoped per booster rather than per card because a card can travel through
    two of them: Cogwork Librarian is drafted out of one pack and put into
    another, and each pack has to be labelled by the pick that took it from
    that pack.
    """
    return {
        booster.index: {
            card: {"pick": step.pick, "seat": step.seat}
            for step in booster.steps for card in step.taken
        }
        for booster in result.boosters
    }
