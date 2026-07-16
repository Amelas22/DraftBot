from helpers.team_display import team_labels, TEAM_A_COLOR, TEAM_B_COLOR


def test_named_teams_are_color_coded():
    a, b = team_labels("premade", "Goblins", "Elves")
    assert a == f"{TEAM_A_COLOR} Goblins"
    assert b == f"{TEAM_B_COLOR} Elves"


def test_random_drafts_use_generic_colored_labels():
    a, b = team_labels("random", None, None)
    assert a == f"{TEAM_A_COLOR} Team Red"
    assert b == f"{TEAM_B_COLOR} Team Blue"


def test_staked_drafts_use_generic_colored_labels():
    a, b = team_labels("staked", "ignored", "ignored")
    assert a == f"{TEAM_A_COLOR} Team Red"
    assert b == f"{TEAM_B_COLOR} Team Blue"


def test_named_teams_fall_back_when_name_missing():
    a, b = team_labels("premade", None, None)
    assert a == f"{TEAM_A_COLOR} Team A"
    assert b == f"{TEAM_B_COLOR} Team B"


def test_teamless_types_use_generic_labels_not_names():
    # swiss/winston have no teams; they still reach livedrafts' embed. They must
    # NOT be treated as named-team drafts (would show a bogus "Team A"/"Team B").
    for team_less in ("swiss", "winston"):
        a, b = team_labels(team_less, None, None)
        assert a == f"{TEAM_A_COLOR} Team Red", team_less
        assert b == f"{TEAM_B_COLOR} Team Blue", team_less


def test_unknown_session_type_defaults_to_generic_labels():
    a, b = team_labels(None, "Goblins", "Elves")
    assert a == f"{TEAM_A_COLOR} Team Red"
    assert b == f"{TEAM_B_COLOR} Team Blue"


def test_red_is_team_a_blue_is_team_b():
    # The color-to-team contract add_sub's Red/Blue option relies on.
    assert TEAM_A_COLOR == "🔴"
    assert TEAM_B_COLOR == "🔵"
