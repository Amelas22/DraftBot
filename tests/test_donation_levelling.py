"""The levelling rule, as a pure function both draft kinds share."""
from services.draft_pool_service import plan_levelling


def test_the_heavier_side_levels_onto_a_common_ceiling():
    held = {"a1": 100, "a2": 50, "b1": 50, "b2": 50}

    plan = plan_levelling(["a1", "a2"], ["b1", "b2"], held, dict(held), set())

    assert plan["matched"] == 100
    assert plan["refunded"] == {"a1": 50}
    assert plan["capped"] == {}
    assert {p: plan["held"][p] for p in held} == {"a1": 50, "a2": 50, "b1": 50, "b2": 50}


def test_a_capped_player_is_trimmed_to_their_share_of_their_own_side_first():
    # a1 asked to be capped: beside a 50 teammate the ceiling is 60
    # (50 * 0.55 / 0.45 = 61, snapped down to a whole ten).
    held = {"a1": 100, "a2": 50, "b1": 50, "b2": 50}

    plan = plan_levelling(["a1", "a2"], ["b1", "b2"], held, dict(held), {"a1"})

    assert plan["capped"] == {"a1": 40}
    assert plan["refunded"] == {"a1": 10}
    assert plan["held"]["a1"] == 50


def test_planning_never_mutates_what_it_was_given():
    held = {"a1": 100, "a2": 50, "b1": 50, "b2": 50}
    before = dict(held)

    plan_levelling(["a1", "a2"], ["b1", "b2"], held, dict(held), set())

    assert held == before


def test_a_player_with_no_entry_is_on_neither_side():
    held = {"a1": 50, "b1": 50}

    plan = plan_levelling(["a1", "a2"], ["b1"], held, dict(held), set())

    assert plan["matched"] == 50 and "a2" not in plan["held"]
