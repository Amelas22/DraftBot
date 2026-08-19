"""Pure state/rendering tests for a tournament match's control message."""
from helpers.match_control import (
    DRAFTING,
    RECORDED,
    SCHEDULING,
    launch_block_text,
    match_state,
    recorded_result_line,
    render_match_control,
)

LINK = "https://discord.com/channels/1/2/3"


def test_no_result_no_draft_is_scheduling():
    assert match_state(False, False) == SCHEDULING


def test_linked_draft_is_drafting():
    assert match_state(False, True) == DRAFTING


def test_result_is_recorded():
    assert match_state(True, False) == RECORDED


def test_result_wins_over_a_still_linked_draft():
    # A finished draft's row lives on until cleanup reaps it, so both facts are
    # true at once; the match is finished, not still drafting.
    assert match_state(True, True) == RECORDED


def test_scheduling_body_names_both_teams_and_invites_start():
    body = render_match_control(SCHEDULING, "Alpha", "Bravo", 2)
    assert "Round 2" in body and "Alpha" in body and "Bravo" in body
    assert "Start draft" in body


def test_drafting_body_links_the_lobby():
    body = render_match_control(DRAFTING, "Alpha", "Bravo", 2, lobby_link=LINK)
    assert LINK in body
    assert "Draft in progress" in body


def test_recorded_body_shows_the_score():
    body = render_match_control(RECORDED, "Alpha", "Bravo", 2, result=(2, 1))
    assert "Result recorded" in body
    assert "2–1" in body


def test_recorded_result_line_orders_names_with_scores():
    line = recorded_result_line("Alpha", "Bravo", 2, 1)
    assert line == "✅ Result recorded: **Alpha** 2–1 **Bravo**"


def test_no_block_when_scheduling():
    assert launch_block_text(SCHEDULING, None, "irrelevant") is None


def test_block_while_drafting_points_at_the_lobby():
    text = launch_block_text(DRAFTING, LINK, "irrelevant")
    assert LINK in text
    assert "already underway" in text


def test_block_while_drafting_survives_a_missing_lobby_link():
    text = launch_block_text(DRAFTING, None, "irrelevant")
    assert "already underway" in text
    assert "None" not in text


def test_block_when_recorded_repeats_the_result():
    text = launch_block_text(RECORDED, None, "✅ Result recorded: **Alpha** 2–1 **Bravo**")
    assert "Result recorded" in text
    assert "admin" in text
