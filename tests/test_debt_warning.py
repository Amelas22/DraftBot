"""Tests for the staked-signup debt warning marker and Sign-Ups formatter."""
import sys

from loguru import logger

from helpers.debt_warning import debt_warning_suffix, format_staked_sign_ups


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
        "1": {"amount": 50, "is_capped": True},
        "2": {"amount": 100, "is_capped": False},
    }
    out = _fmt(sign_ups, stakes, pool=150)
    names = [line for line in out.split("\n")[1:]]
    assert names == ["Alice", "Bob", "❌ Carol has not set a bet"], out


def test_the_queue_shows_the_pool_not_each_players_bet():
    """One number people care about, instead of six they do not.

    A winner takes double their matched stake, so the most the table can play
    for is what everyone has put in -- and that is the figure worth showing
    while the queue fills.
    """
    sign_ups = {"1": "Alice", "2": "Bob"}
    stakes = {"1": {"amount": 50, "is_capped": True},
              "2": {"amount": 100, "is_capped": False}}

    out = _fmt(sign_ups, stakes, pool=150)

    assert out.startswith("**Players (2)** — prize pool: up to 150 tix"), out
    assert "50 tix:" not in out and "100 tix:" not in out, (
        f"a player's own bet is still on show: {out!r}")
    assert "🧢" not in out and "🏎️" not in out, (
        "the bet-cap markers outlived the matcher that read them")


def test_a_pool_of_nothing_is_not_advertised():
    """An empty queue has no pool to name."""
    out = _fmt({}, {}, pool=0)
    assert out == "**Players (0):**\nNo players yet.", out


def test_flagged_player_gets_suffix():
    sign_ups = {"1": "Alice", "2": "Bob"}
    stakes = {"1": {"amount": 50, "is_capped": True},
              "2": {"amount": 20, "is_capped": True}}
    # Alice: 150 total, 120 of it old -> warns, displays the 150 total.
    # Bob: 130 total but only 40 old -> under the bar, no marker.
    out = _fmt(sign_ups, stakes, owed={"1": 150, "2": 130},
               old_owed={"1": 120, "2": 40}, threshold=100)
    assert "Alice ⚠️ owes 150 tix" in out
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
