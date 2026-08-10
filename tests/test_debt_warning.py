"""Tests for the staked-signup debt warning marker and Sign-Ups formatter."""
import sys

from loguru import logger

from helpers.debt_warning import debt_warning_suffix, format_staked_sign_ups


def _fmt(sign_ups, stakes, owed=None, old_owed=None, threshold=100):
    return format_staked_sign_ups(
        sign_ups, stakes, owed or {}, old_owed if old_owed is not None else (owed or {}),
        threshold,
        display_name_for=lambda uid, stored: stored,
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

def test_parity_with_legacy_format_when_no_debts():
    """Byte-identical to the pre-extraction update_draft_message output."""
    sign_ups = {"1": "Alice", "2": "Bob", "3": "Carol"}
    stakes = {
        "1": {"amount": 50, "is_capped": True},
        "2": {"amount": 100, "is_capped": False},
    }
    out = _fmt(sign_ups, stakes)
    assert out == (
        "**Players (3):**\n"
        "🏎️ 100 tix: Bob\n"      # uncapped, highest stake first
        "🧢 50 tix: Alice\n"
        "❌ Not set: Carol"        # "Not set" sorts last
    )


def test_flagged_player_gets_suffix():
    sign_ups = {"1": "Alice", "2": "Bob"}
    stakes = {"1": {"amount": 50, "is_capped": True},
              "2": {"amount": 20, "is_capped": True}}
    # Alice: 150 total, 120 of it old -> warns, displays the 150 total.
    # Bob: 130 total but only 40 old -> under the bar, no marker.
    out = _fmt(sign_ups, stakes, owed={"1": 150, "2": 130},
               old_owed={"1": 120, "2": 40}, threshold=100)
    assert "🧢 50 tix: Alice ⚠️ owes 150 tix" in out
    assert "🧢 20 tix: Bob" in out and "Bob ⚠️" not in out


def test_not_set_line_can_carry_suffix():
    out = _fmt({"1": "Alice"}, {}, owed={"1": 190}, threshold=100)
    assert out == "**Players (1):**\n❌ Not set: Alice ⚠️ owes 190 tix"


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
