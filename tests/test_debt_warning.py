"""Tests for the staked-signup debt warning marker and Sign-Ups formatter."""
from helpers.debt_warning import debt_warning_suffix, format_staked_sign_ups


def _fmt(sign_ups, stakes, owed=None, threshold=50):
    return format_staked_sign_ups(
        sign_ups, stakes, owed or {}, threshold,
        display_name_for=lambda uid, stored: stored,
    )


# ---- debt_warning_suffix -------------------------------------------------------------

def test_suffix_at_and_above_threshold():
    assert debt_warning_suffix(50, 50) == " ⚠️ owes 50 tix"
    assert debt_warning_suffix(75, 50) == " ⚠️ owes 75 tix"


def test_suffix_below_threshold_or_no_debt():
    assert debt_warning_suffix(49, 50) == ""
    assert debt_warning_suffix(0, 50) == ""
    assert debt_warning_suffix(None, 50) == ""


def test_suffix_threshold_zero_disables():
    assert debt_warning_suffix(1000, 0) == ""


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
    out = _fmt(sign_ups, stakes, owed={"1": 75, "2": 30}, threshold=50)
    assert "🧢 50 tix: Alice ⚠️ owes 75 tix" in out
    assert "🧢 20 tix: Bob" in out and "Bob ⚠️" not in out


def test_not_set_line_can_carry_suffix():
    out = _fmt({"1": "Alice"}, {}, owed={"1": 90}, threshold=50)
    assert out == "**Players (1):**\n❌ Not set: Alice ⚠️ owes 90 tix"


def test_empty_signups():
    assert _fmt({}, {}) == "**Players (0):**\nNo players yet."


def test_overflow_logs_warning(capsys):
    from loguru import logger
    import sys
    logger.add(sys.stderr, level="WARNING")
    sign_ups = {str(i): "N" * 60 for i in range(20)}   # force > 1000 chars
    _fmt(sign_ups, {})
    assert "exceeds single-field limit" in capsys.readouterr().err
