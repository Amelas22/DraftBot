"""Pure bracket maths: layout, advancement, placement."""
import pytest

from draft_organization.bracket import bracket_size, build_bracket


def test_bracket_size_rounds_up_to_a_power_of_two():
    assert bracket_size(4) == 4
    assert bracket_size(5) == 8
    assert bracket_size(6) == 8
    assert bracket_size(8) == 8
    assert bracket_size(9) == 16


def test_top_four_has_no_byes():
    assert build_bracket(4) == [(1, 4), (2, 3)]


def test_top_eight_is_in_bracket_order_not_seed_order():
    # (1,8) (4,5) (2,7) (3,6) -- NOT (1,8) (2,7) (3,6) (4,5). The order is
    # load-bearing: the next round pairs ADJACENT winners, so seed-sorted
    # pairings would put 1 and 2 together in the semifinal.
    assert build_bracket(8) == [(1, 8), (4, 5), (2, 7), (3, 6)]


def test_top_six_gives_byes_to_the_top_two_seeds():
    assert build_bracket(6) == [(1, None), (4, 5), (2, None), (3, 6)]


def test_bracket_needs_at_least_two_seeds():
    with pytest.raises(ValueError):
        build_bracket(1)
