"""Unit tests for reorder_sign_ups — the shared 'reorder sign_ups by an ordered
id list' helper used by the premade and swiss seating paths. It reorders by user
id (never by display name), which is what keeps decorated/escaped names from
breaking the mapping (see PR #349)."""

from utils import reorder_sign_ups


def test_reorder_sign_ups_orders_by_id_and_preserves_names():
    sign_ups = {"1": "Alice", "2": "Bob", "3": "Carol"}
    out = reorder_sign_ups(sign_ups, ["3", "1", "2"])
    assert list(out.keys()) == ["3", "1", "2"]      # reordered to the id list
    assert out == {"3": "Carol", "1": "Alice", "2": "Bob"}   # names unchanged


def test_reorder_sign_ups_returns_a_new_dict():
    sign_ups = {"1": "Alice"}
    out = reorder_sign_ups(sign_ups, ["1"])
    assert out is not sign_ups


def test_reorder_sign_ups_keys_by_id_even_when_names_collide():
    # two players with the same display name must not collapse — keyed by id
    sign_ups = {"1": "Sam", "2": "Sam"}
    out = reorder_sign_ups(sign_ups, ["2", "1"])
    assert list(out.keys()) == ["2", "1"]
    assert len(out) == 2
