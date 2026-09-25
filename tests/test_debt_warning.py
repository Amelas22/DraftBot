"""Tests for the staked-signup debt warning marker and Sign-Ups formatter."""
import sys

from loguru import logger

from helpers.debt_warning import (debt_warning_suffix, format_staked_sign_ups,
                                  shown_stake)


def _fmt(sign_ups, stakes, owed=None, old_owed=None, threshold=100, pool=0):
    return format_staked_sign_ups(
        sign_ups, stakes, owed or {}, old_owed if old_owed is not None else (owed or {}),
        threshold,
        display_name_for=lambda uid, stored: stored,
        pool=pool,
    )


# ---- debt_warning_suffix -------------------------------------------------------------

def test_suffix_displays_total_but_triggers_on_old_debt():
    assert debt_warning_suffix(150, 120, 100) == " ⚠️ owes 150 tix"


def test_suffix_strictly_greater_than_threshold():
    assert debt_warning_suffix(150, 100, 100) == ""      # exactly at: no warning
    assert debt_warning_suffix(150, 101, 100) == " ⚠️ owes 150 tix"


def test_suffix_no_old_debt_means_no_warning_even_with_large_total():
    assert debt_warning_suffix(500, 0, 100) == ""
    assert debt_warning_suffix(500, None, 100) == ""


def test_suffix_threshold_zero_disables():
    assert debt_warning_suffix(1000, 900, 0) == ""


# ---- format_staked_sign_ups ----------------------------------------------------------

def test_the_queue_lists_players_in_join_order():
    """Sorting the queue by stake turns signing up into a leaderboard.

    It puts the biggest bet at the top of every draft and tells everyone what
    everyone else is in for before they choose their own. Join order is the
    fact the queue is actually reporting -- who got here first.
    """
    sign_ups = {"1": "Alice", "2": "Bob", "3": "Carol"}
    stakes = {
        "1": {"amount": 150, "is_capped": True},
        "2": {"amount": 100, "is_capped": False},
    }
    out = _fmt(sign_ups, stakes, pool=150)
    names = [line for line in out.split("\n")[1:]]
    assert names == ["Alice 100+", "Bob 100+", "❌ Carol has not set a bet"], out


def test_the_queue_shows_the_pool_as_well_as_the_bets():
    """Both: the pot is what the table plays for, and the bets are how
    somebody still choosing works out what will actually be matched.

    The bets came back deliberately after a spell without them. What made
    them a leaderboard was exact figures sorted by size; join order and a
    shared top bucket remove that, and what is left -- reading the room
    before you choose -- is the thing worth having, because money that
    nobody matches is just money that bounces back.
    """
    sign_ups = {"1": "Alice", "2": "Bob"}
    stakes = {"1": {"amount": 50, "is_capped": True},
              "2": {"amount": 300, "is_capped": False}}

    out = _fmt(sign_ups, stakes, pool=150)

    assert out.startswith("**Players (2)** — prize pool: up to 150 tix"), out
    assert "Alice 50" in out and "Bob 100+" in out
    assert "300" not in out, (
        f"an over-ceiling bet showed its own figure: {out!r}")
    assert "🧢" not in out and "🏎️" not in out, (
        "the board advertised a per-player cap setting nobody else can act on")


def test_a_pool_of_nothing_is_not_advertised():
    """An empty queue has no pool to name."""
    out = _fmt({}, {}, pool=0)
    assert out == "**Players (0):**\nNo players yet.", out


def test_flagged_player_gets_suffix():
    sign_ups = {"1": "Alice", "2": "Bob"}
    stakes = {"1": {"amount": 150, "is_capped": True},
              "2": {"amount": 20, "is_capped": True}}
    # Alice: 150 total, 120 of it old -> warns, displays the 150 total.
    # Bob: 130 total but only 40 old -> under the bar, no marker.
    out = _fmt(sign_ups, stakes, owed={"1": 150, "2": 130},
               old_owed={"1": 120, "2": 40}, threshold=100)
    assert "Alice 100+ ⚠️ owes 150 tix" in out
    assert "Bob" in out and "Bob ⚠️" not in out


def test_not_set_line_can_carry_suffix():
    out = _fmt({"1": "Alice"}, {}, owed={"1": 190}, threshold=100)
    assert out == "**Players (1):**\n❌ Alice has not set a bet ⚠️ owes 190 tix"


def test_empty_signups():
    assert _fmt({}, {}) == "**Players (0):**\nNo players yet."


def test_overflow_logs_warning(capsys):
    hid = logger.add(sys.stderr, level="WARNING")
    try:
        sign_ups = {str(i): "N" * 60 for i in range(20)}   # force > 1000 chars
        _fmt(sign_ups, {})
        assert "exceeds single-field limit" in capsys.readouterr().err
    finally:
        logger.remove(hid)


# ---- bets on the signup board ---------------------------------------------

def test_a_small_bet_shows_its_exact_amount():
    out = _fmt({"1": "Ava"}, {"1": _stake(20)})

    assert "Ava 20" in out


def test_a_large_bet_shows_only_that_it_is_large():
    """Above the ceiling every bet reads the same, so the top of the table
    cannot be ranked. Somebody in for 300 and somebody in for 100 are both
    "100+", which is all anyone else needs to know while choosing their own."""
    out = _fmt({"1": "Dev", "2": "Eli"}, {"1": _stake(300), "2": _stake(100)})

    assert "Dev 100+" in out and "Eli 100+" in out
    assert "300" not in out


def test_the_ceiling_boundary_is_exact():
    """One below the ceiling is a figure, the ceiling itself is the bucket.

    The only edge here that can silently regress -- a `>` for a `>=` moves it
    by one and nothing else in the suite would notice.
    """
    assert shown_stake(99) == "99"
    assert shown_stake(100) == "100+"


def test_the_ceiling_itself_is_already_large():
    assert "100+" in _fmt({"1": "Ava"}, {"1": _stake(100)})
    assert "Ava 50" in _fmt({"1": "Ava"}, {"1": _stake(50)}), "below it, the figure shows"


def test_players_stay_in_join_order():
    """Never sorted by size. Sorting is what made this a leaderboard before,
    with the largest bet sitting at the top of every draft."""
    out = _fmt({"1": "Ava", "2": "Dev", "3": "Cora"},
               {"1": _stake(20), "2": _stake(300), "3": _stake(20)})
    names = [ln.split()[0] for ln in out.splitlines()[1:]]

    assert names == ["Ava", "Dev", "Cora"]


def test_somebody_with_no_bet_still_reads_as_missing():
    out = _fmt({"1": "Ava", "2": "Ben"}, {"1": _stake(20)})

    assert "❌ Ben has not set a bet" in out
    assert "Ava 20" in out


def test_a_debt_warning_survives_alongside_the_bet():
    out = _fmt({"1": "Ava"}, {"1": _stake(20)}, owed={"1": 150}, old_owed={"1": 150},
               threshold=100)

    assert "Ava 20" in out and "owes 150 tix" in out


def _stake(amount, is_capped=True):
    """The shape views.py builds: a plain dict, not a StakeInfo row."""
    return {"amount": amount, "is_capped": is_capped}
