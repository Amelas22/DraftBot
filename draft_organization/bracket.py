"""Pure single-elimination bracket maths: layout, advancement, placement.

No database and no Discord. Seeds are 1-based ints; participant ids are
opaque. Sits beside swiss.py, which owns the pairing maths for the rounds
that decide these seeds.
"""


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
