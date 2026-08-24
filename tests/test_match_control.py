"""Pure state/rendering tests for a tournament match's control message."""
from helpers.match_control import (
    DRAFTING,
    RECORDED,
    SCHEDULING,
    launch_block_text,
    match_state,
    recorded_result_line,
    render_match_control,
    render_pairing_line,
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
    body = render_match_control(SCHEDULING, "Alpha", "Bravo", "Round 2")
    assert "Round 2" in body and "Alpha" in body and "Bravo" in body
    assert "Start draft" in body


def test_drafting_body_links_the_lobby():
    body = render_match_control(DRAFTING, "Alpha", "Bravo", "Round 2", lobby_link=LINK)
    assert LINK in body
    assert "Draft in progress" in body


def test_recorded_body_shows_the_score():
    body = render_match_control(RECORDED, "Alpha", "Bravo", "Round 2", result=(2, 1))
    assert "Result recorded" in body
    assert "2–1" in body


def test_control_body_mentions_both_team_roles_when_they_exist():
    """Mentioning a role adds those members to the thread, and that membership
    is what surfaces the match in each player's sidebar. A mention that did not
    add them would only be a notification."""
    body = render_match_control(
        SCHEDULING, "Alpha", "Bravo", "Round 1",
        role_mentions=("111", "222"),
    )
    assert "<@&111>" in body and "<@&222>" in body


def test_control_body_is_unchanged_without_roles():
    """role_id is NULL for tournaments that predate this feature; they must
    render exactly as they do today."""
    without_roles = render_match_control(SCHEDULING, "Alpha", "Bravo", "Round 1")
    assert "<@&" not in without_roles


def test_one_team_without_a_role_mentions_neither():
    """Half a tagged match is worse than none: one team pulled into the thread
    and the other not, with nothing on the message saying so."""
    body = render_match_control(
        SCHEDULING, "Alpha", "Bravo", "Round 1", role_mentions=("111", None),
    )
    assert "<@&" not in body


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


def test_pairing_line_without_a_thread_is_just_the_teams():
    line = render_pairing_line("Alpha", "Bravo")
    assert line == "• **Alpha** vs **Bravo**"


def test_pairing_line_links_the_thread_when_there_is_one():
    line = render_pairing_line("Alpha", "Bravo", thread_id="900")
    assert "<#900>" in line
    assert "Alpha" in line and "Bravo" in line


def test_pairing_line_shows_the_result_once_recorded():
    line = render_pairing_line("Alpha", "Bravo", thread_id="900", result=(2, 1))
    assert "<#900>" in line
    assert "✅ Result recorded: **Alpha** 2–1 **Bravo**" in line


def test_pairing_line_with_an_unplayed_result_pair_stays_unrecorded():
    # (None, None) is what an unplayed match carries; it must not render a score.
    line = render_pairing_line("Alpha", "Bravo", thread_id="900", result=(None, None))
    assert "Result recorded" not in line
