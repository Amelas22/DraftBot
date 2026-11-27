"""Seat names for a hosted Draftmancer session.

A hosted session holds one named seat per sign-up, and a player's link binds
them to the seat by name, so every seat name must be unique. Display names are
not: two players can share one. The first keeps the display name; each repeat
gets " (2)", " (3)"... -- never a name another player actually has.

Everything that sends players to the hosted server, compares its seats with
the sign-ups, or builds a player's link goes through seat_names, so they all
agree on who sits where.
"""
from __future__ import annotations

from collections.abc import Mapping


def seat_names(sign_ups: Mapping[str, str]) -> dict[str, str]:
    """Discord id -> unique seat name, in sign-up (seat) order."""
    raw = set(sign_ups.values())
    taken: set[str] = set()
    seats: dict[str, str] = {}
    for discord_id, name in sign_ups.items():
        candidate, n = name, 1
        # A made-up name must not be one a real player has, even a later one.
        while candidate in taken or (n > 1 and candidate in raw):
            n += 1
            candidate = f"{name} ({n})"
        taken.add(candidate)
        seats[discord_id] = candidate
    return seats


def seat_list(sign_ups: Mapping[str, str]) -> list[str]:
    """The seat names in seat order: what the hosted server is sent."""
    return list(seat_names(sign_ups).values())


def discord_ids_by_seat(sign_ups: Mapping[str, str]) -> dict[str, str]:
    """Seat name -> Discord id, the inverse of seat_names."""
    return {seat: discord_id for discord_id, seat in seat_names(sign_ups).items()}
