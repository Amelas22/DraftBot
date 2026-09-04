"""Reconstruction of every physical booster's journey around the table.

Uses a real stored Draftmancer log (experimental-91, a 6-seat 3-pack PowerLSV
draft) rather than a synthetic fixture: the interesting failure modes here --
per-pack passing direction and Cogwork Librarian putting a card back into a
booster mid-flight -- are exactly the things a hand-built fixture would omit.
"""
import json
from pathlib import Path

import pytest

# The draft log ships as a committed test fixture, not the (gitignored)
# analysis_data cache -- so these tests run on a clean checkout too.
ROOT = Path(__file__).resolve().parent.parent

import services.draft_reconstruct as draft_reconstruct  # noqa: E402
from services.draft_reconstruct import (  # noqa: E402
    card_fates, pack_directions, passing_ring, reconstruct,
)

LOG = ROOT / "tests" / "fixtures" / "draft_log_6seat.json"

# Verified against the raw log by content-chaining every booster.
RING = ["Strider / osmanozguney", "Sven745", "Birb // Entropy263",
        "daxerz", "Adham / aemomen", "AlicanGokturk"]
SEATS = len(RING)


@pytest.fixture(scope="module")
def draft():
    if not LOG.exists():
        pytest.skip(f"draft log not cached at {LOG}")
    return json.loads(LOG.read_text())


def test_passing_ring_is_pack_one_seating_order(draft):
    """The ring is derived from who received whose pack-1 leftovers."""
    assert passing_ring(draft) == RING


def test_ring_starts_at_the_requested_seat(draft):
    """Any seat can be the ring's origin; the cycle order is unchanged."""
    rotated = passing_ring(draft, start="daxerz")
    assert rotated[0] == "daxerz"
    assert rotated == RING[3:] + RING[:3]


def test_packs_alternate_passing_direction(draft):
    """Pack 1 left, pack 2 right, pack 3 left -- read from the log, not assumed."""
    assert pack_directions(draft) == [1, -1, 1]


def test_every_booster_is_reconstructed_end_to_end(draft):
    """6 seats x 3 packs = 18 boosters, each passing through all 15 picks."""
    boosters = reconstruct(draft).boosters
    assert len(boosters) == SEATS * 3
    assert {len(b.steps) for b in boosters} == {15}


def test_booster_contents_chain_between_consecutive_holders(draft):
    """What a seat passes on is exactly what the next seat receives.

    This is the invariant the whole viewer rests on: if it holds, the card
    grid shown at every step is the real booster, not a guess.
    """
    for booster in reconstruct(draft).boosters:
        for earlier, later in zip(booster.steps, booster.steps[1:]):
            passed = set(earlier.contents) - set(earlier.taken)
            # A seat can also add a card as it picks (Cogwork Librarian), and
            # Draftmancer records that seat's booster with the card already in.
            assert passed | set(later.inserted) == set(later.contents), (
                f"booster {booster.index} broke passing "
                f"{earlier.seat} -> {later.seat} at pick {earlier.pick}"
            )


def test_booster_returns_to_its_opener_on_the_wheel(draft):
    """With six seats a booster comes back to whoever opened it at picks 7 and 13."""
    for booster in reconstruct(draft).boosters:
        seats_at = {step.pick: step.seat for step in booster.steps}
        assert seats_at[1] == booster.opener
        assert seats_at[1 + SEATS] == booster.opener
        assert seats_at[1 + 2 * SEATS] == booster.opener


def test_cogwork_librarian_pick_takes_two_cards_and_returns_the_librarian(draft):
    """Sven745 exiled Cogwork Librarian at pack 3 pick 4 to draft two cards.

    The Librarian goes back into that booster, so the pack grows by one card
    at the same step it loses two -- the one place naive chaining fails.
    """
    steps = [s for b in reconstruct(draft).boosters for s in b.steps
             if s.pack == 3 and s.pick == 4 and s.seat == "Sven745"]
    assert len(steps) == 1
    step = steps[0]
    assert len(step.taken) == 2
    assert {step.card_name(c) for c in step.taken} == {"Deep-Cavern Bat", "Oust"}
    assert [step.card_name(c) for c in step.inserted] == ["Cogwork Librarian"]


def test_only_the_librarian_step_is_flagged_as_an_event(draft):
    """Events are rare by construction, so the viewer can mark them on the clock."""
    events = reconstruct(draft).events
    assert len(events) == 1
    assert events[0].seat == "Sven745"
    assert events[0].pack == 3 and events[0].pick == 4
    assert "Cogwork Librarian" in events[0].description


def test_card_fate_records_who_took_it_and_when(draft):
    """Black Lotus opened by Strider in pack 2 and taken by him immediately."""
    fate = card_fates(reconstruct(draft))["Black Lotus"]
    assert fate.taken_by == "Strider / osmanozguney"
    assert (fate.pack, fate.taken_at_pick) == (2, 1)
    assert fate.wheeled is False


def test_card_fate_marks_a_card_that_survived_the_wheel(draft):
    """Cogwork Librarian re-entered the pack and went round to the last pick."""
    fate = card_fates(reconstruct(draft))["Cogwork Librarian"]
    assert fate.taken_by == "Strider / osmanozguney"
    assert (fate.pack, fate.taken_at_pick) == (3, 15)
    assert fate.wheeled is True


def test_the_librarian_is_the_only_card_drafted_twice(draft):
    """Every card is taken once, except the one that was put back in a pack."""
    result = reconstruct(draft)
    taken = [c for b in result.boosters for s in b.steps for c in s.taken]
    duplicated = {c for c in taken if taken.count(c) > 1}
    assert [result.card_name(c) for c in duplicated] == ["Cogwork Librarian"]


def test_each_card_has_one_fate_matching_the_pools(draft):
    """One fate per distinct card, and it agrees with where the card ended up.

    A card drafted twice keeps its final taker, so the fates are a faithful
    index of the finished pools rather than of pick events.
    """
    result = reconstruct(draft)
    fates = card_fates(result)
    pools = {seat["userName"]: set(seat.get("cards") or [])
             for seat in draft["users"].values()}
    assert len(fates) == sum(len(pool) for pool in pools.values())
    for fate in fates.values():
        assert fate.card_id in pools[fate.taken_by], (
            f"{fate.name} recorded to {fate.taken_by} but is not in their pool"
        )


def test_seat_pool_ends_as_the_finished_pool(draft):
    """Folding every pick a seat made reproduces the pool the log recorded.

    The entries are the full history, spent cards included, so what has to
    match the finished pool is the cards still held at the end.
    """
    result = reconstruct(draft)
    pools = draft_reconstruct.seat_pools(result)
    for seat in draft["users"].values():
        name = str(seat["userName"])
        held = [e["card"] for e in pools[name] if e["gone"] is None]
        assert set(held) == set(seat["cards"]), f"{name}'s folded pool differs"
        assert len(held) == len(seat["cards"])


def test_pool_through_grows_one_card_per_pick(draft):
    """Stepping the clock adds exactly the card taken at that step."""
    result = reconstruct(draft)
    seat = "Strider / osmanozguney"
    assert len(draft_reconstruct.pool_through(result, seat, 1, 1)) == 1
    assert len(draft_reconstruct.pool_through(result, seat, 1, 5)) == 5
    assert len(draft_reconstruct.pool_through(result, seat, 2, 1)) == 16
    assert len(draft_reconstruct.pool_through(result, seat, 3, 15)) == 45


def test_an_exiled_card_leaves_the_pool_it_was_drafted_into(draft):
    """Sven745 held Cogwork Librarian for two picks, then spent it.

    A pool that only ever adds cards would still show it in his pile for the
    rest of the draft, and would double-count it against Strider's.
    """
    result = reconstruct(draft)
    names = lambda ids: {result.card_name(c) for c in ids}
    assert "Cogwork Librarian" in names(
        draft_reconstruct.pool_through(result, "Sven745", 3, 3))
    assert "Cogwork Librarian" not in names(
        draft_reconstruct.pool_through(result, "Sven745", 3, 4))
    assert "Cogwork Librarian" not in names(
        draft_reconstruct.pool_through(result, "Sven745", 3, 15))


def _card_id(result, name):
    for card_id, card in result.carddata.items():
        if card.get("name") == name:
            return card_id
    raise AssertionError(f"{name} not in this draft")


def test_taken_in_labels_each_booster_by_its_own_picks(draft):
    """A card in two boosters is labelled by the pick that took it *there*.

    Cogwork Librarian sat in booster #13 until Sven745 took it at pick 2, then
    he put it into booster #17 where Strider took it at pick 15. Reusing the
    card's overall fate would stamp "pick 15, Strider" onto booster #13 too.
    """
    result = reconstruct(draft)
    taken = draft_reconstruct.taken_in(result)
    librarian = _card_id(result, "Cogwork Librarian")
    assert taken[13][librarian] == {"pick": 2, "seat": "Sven745"}
    assert taken[17][librarian] == {"pick": 15, "seat": "Strider / osmanozguney"}


def test_every_card_in_a_booster_is_eventually_taken_from_it(draft):
    """Ordering by take-pick needs a pick for every card the viewer draws."""
    result = reconstruct(draft)
    taken = draft_reconstruct.taken_in(result)
    for booster in result.boosters:
        seen = {card for step in booster.steps for card in step.contents}
        assert seen == set(taken[booster.index]), (
            f"booster {booster.index} has cards with no recorded take"
        )
