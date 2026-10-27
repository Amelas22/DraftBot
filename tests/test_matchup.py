from datetime import datetime, timezone

from helpers.matchup import (
    OPEN,
    SETTING_UP,
    STARTED,
    Booking,
    Side,
    assign_people,
    booking_announcement,
    claimed_state,
    discord_time,
    launch_block,
    matchup_name,
    matchup_state,
    render_matchup_control,
    side_label,
)


def test_a_single_role_side_is_named_after_the_role():
    assert side_label(["Team Garruk"], []) == "Team Garruk"


def test_a_mixed_side_lists_every_name():
    assert side_label(["Team Garruk"], ["Ash"]) == "Team Garruk, Ash"


def test_a_missing_role_keeps_the_stand_in_name():
    assert side_label(["deleted role"], []) == "deleted role"
    assert side_label(["deleted role"], ["Ash"]) == "deleted role, Ash"


def test_a_long_matchup_name_fits_a_thread():
    a = Side("x" * 80, (), ("1",))
    b = Side("y" * 80, (), ("2",))
    assert len(matchup_name(a, b)) == 100


def test_a_short_matchup_name_is_a_vs_b():
    assert matchup_name(Side("A", (), ()), Side("B", (), ())) == "A vs B"


def test_mentions_list_roles_then_users():
    assert Side("A", ("1", "2"), ("3",)).mentions() == "<@&1> <@&2> <@3>"


def test_discord_time_takes_aware_and_naive_utc():
    aware = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 10, 30, 0, 0)
    expected = "<t:1793318400:F> (<t:1793318400:R>)"
    assert discord_time(aware) == expected
    assert discord_time(naive) == expected


def test_state_follows_whether_a_draft_exists():
    assert matchup_state(False) == OPEN
    assert matchup_state(True) == STARTED


def test_control_says_no_time_until_booked_then_shows_it():
    a, b = Side("A", ("1",), ()), Side("B", (), ("2",))
    assert "No time booked yet" in render_matchup_control(
        a, b, Booking(None, None), OPEN, "Ann", None
    )
    t = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)
    assert "<t:1793318400:F>" in render_matchup_control(
        a, b, Booking(t, "Bo"), OPEN, "Ann", None
    )


def test_control_names_the_sides_and_who_set_it_up():
    a, b = Side("A", ("1",), ()), Side("B", (), ("2",))
    text = render_matchup_control(a, b, Booking(None, None), OPEN, "Ann", None)
    assert "A vs B" in text
    assert "<@&1>" in text and "<@2>" in text
    assert "Set up by Ann" in text


def test_control_reports_a_started_draft_with_its_link():
    a, b = Side("A", (), ("1",)), Side("B", (), ("2",))
    started = render_matchup_control(a, b, Booking(None, None), STARTED, "Ann", "L")
    assert "Draft started" in started and "L" in started
    played = render_matchup_control(a, b, Booking(None, None), STARTED, "Ann", None)
    assert "Draft played." in played


def test_a_started_matchup_blocks_with_the_lobby_link():
    assert "join it here: L" in (launch_block(STARTED, "L") or "")
    assert launch_block(OPEN, None) is None


def test_a_started_matchup_without_a_link_says_it_was_played():
    text = launch_block(STARTED, None)
    assert text is not None and "already been played" in text


def test_announcements_tag_both_sides_and_say_who_and_what():
    a, b = Side("A", ("1",), ()), Side("B", (), ("2",))
    t1 = datetime(2026, 10, 30, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 31, tzinfo=timezone.utc)
    text = booking_announcement("Bo", t1, t2, a, b)
    assert text.splitlines()[0] == "<@&1> <@2>"
    assert "**Bo** moved the draft from <t:1793318400:F>" in text
    assert "cleared" in booking_announcement("Bo", t1, None, a, b)
    assert "**Bo** booked the draft for <t:1793404800:F>" in booking_announcement(
        "Bo", None, t2, a, b
    )


def test_someone_on_both_sides_is_counted_once():
    assert assign_people(["5"], ["1", "2"], ["9"], ["2", "5"]) == (
        ["5", "1", "2"],
        ["9"],
    )
    assert assign_people([], ["1"], ["1"], []) == ([], ["1"])


def test_a_claimed_matchup_is_being_set_up_until_its_lobby_exists_or_it_ran():
    assert claimed_state(has_lobby=False, ran=False) == SETTING_UP
    assert claimed_state(has_lobby=True, ran=False) == STARTED
    assert claimed_state(has_lobby=False, ran=True) == STARTED


def test_a_matchup_being_set_up_says_so_not_played():
    text = launch_block(SETTING_UP, None)
    assert text is not None and "being set up right now" in text
    assert "played" not in text
    a, b = Side("A", (), ("1",)), Side("B", (), ("2",))
    body = render_matchup_control(a, b, Booking(None, None), SETTING_UP, "Ann", None)
    assert "being set up" in body and "played" not in body
