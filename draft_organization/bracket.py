"""Pure single-elimination bracket maths: layout, advancement, placement.

No database and no Discord. Seeds are 1-based ints; participant ids are
opaque. Sits beside swiss.py, which owns the pairing maths for the rounds
that decide these seeds.
"""

from typing import Any


def bracket_size(seed_count: int) -> int:
    """The smallest power of two that seats `seed_count` teams."""
    size = 1
    while size < seed_count:
        size *= 2
    return size


def _seat_order(size: int) -> list[int]:
    """Seat numbers in standard bracket order for a full bracket of `size`.

    Built by repeatedly reflecting: [1] -> [1,2] -> [1,4,2,3] -> ... Each
    step pairs every existing seat against its complement, which is what puts
    seeds 1 and 2 in opposite halves at every depth.
    """
    seats = [1]
    while len(seats) < size:
        complement = len(seats) * 2 + 1
        seats = [x for seat in seats for x in (seat, complement - seat)]
    return seats


def build_bracket(seed_count: int) -> list[tuple[int, int | None]]:
    """First-round pairings, in bracket order.

    `None` as the second element means the seat is empty and that seed has a
    bye. Later rounds are NOT returned: they come from `advance_pairs`, which
    pairs adjacent winners in this same order.
    """
    if seed_count < 2:
        raise ValueError("A bracket needs at least 2 seeds.")
    seats = _seat_order(bracket_size(seed_count))
    pairs: list[tuple[int, int | None]] = []
    for a, b in zip(seats[::2], seats[1::2]):
        high, low = min(a, b), max(a, b)
        pairs.append((high, None if low > seed_count else low))
    return pairs


def advance_pairs(winners: list[Any]) -> list[tuple[Any, Any]]:
    """Pair adjacent winners, in order, to form the next round.

    This is the invariant the whole bracket rests on: the order matches
    `build_bracket`'s output, so pairing neighbours preserves the halves.
    An odd count means a round was mis-built, not a bye -- byes live in the
    first round only.
    """
    if len(winners) % 2:
        raise ValueError(f"Cannot pair {len(winners)} winners into a round.")
    return list(zip(winners[::2], winners[1::2]))


def final_placement(rounds: list[list[tuple[Any, Any | None]]], seeds: dict[Any, int]) -> list[Any]:
    """Bracket teams in finishing order, best first.

    `rounds` is earliest-first; each round is a list of (winner, loser) with
    `loser` None for a bye. Teams eliminated in a later round place higher;
    within a round, ties break by seed, which is why the seed is stored at
    the cut rather than recomputed.

    Also handles an INCOMPLETE bracket: any team that has never lost across
    the rounds given ranks above every eliminated team, ordered by seed.
    When the bracket is complete, exactly one team has never lost, so this
    returns precisely the single-champion result it always did — callers
    passing a full set of rounds see no change in behavior.
    """
    def _seed_key(pid: Any) -> int:
        return seeds.get(pid, len(seeds) + 1)

    if not rounds:
        return []
    eliminated = {loser for rnd in rounds for _, loser in rnd if loser is not None}
    never_lost = {winner for rnd in rounds for winner, _ in rnd} - eliminated
    order: list[Any] = []
    placed: set[Any] = set()

    def _place(pids: list[Any]) -> None:
        """Append these, skipping anyone already placed.

        A corrected result can leave one team recorded as the loser of two
        different rounds. Placing it once — at the deepest round it lost,
        since we walk backwards — keeps this a finishing ORDER; without the
        skip a team appears twice and the payout pays it two prize slots.
        """
        for pid in pids:
            if pid in placed:
                continue
            placed.add(pid)
            order.append(pid)

    _place(sorted(never_lost, key=_seed_key))
    for rnd in reversed(rounds):
        _place(sorted((loser for _, loser in rnd if loser is not None), key=_seed_key))
    return order
