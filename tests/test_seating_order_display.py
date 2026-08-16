"""Seating orders are built from raw sign_ups display names, while the team-name
lists in the SAME embed go through get_display_name_by_id, which escapes
markdown. A player named **Bold**User therefore renders bold in one field and
literal in the other."""
from helpers.display_names import format_seating_order


def test_seating_order_escapes_markdown_like_the_team_lists_beside_it():
    out = format_seating_order(["**Bold**User", "Normal User"])
    assert "**Bold**User" not in out, f"unescaped markdown would render bold: {out!r}"
    assert "Normal User" in out


def test_seating_order_keeps_the_arrow_separator():
    assert format_seating_order(["Alice", "Bob", "Carol"]) == "Alice -> Bob -> Carol"


def test_seating_order_handles_an_empty_roster():
    assert format_seating_order([]) == ""


def test_seating_order_escapes_underscores_and_tildes():
    """The repo's own test users are named _Italic_Name and ~Strike~User."""
    out = format_seating_order(["_Italic_Name", "~Strike~User"])
    assert "_Italic_Name" not in out
    assert "~Strike~User" not in out
