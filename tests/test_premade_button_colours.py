"""The premade sign-up buttons wear their team's colour: 🔴 red, 🔵 blue."""
from types import SimpleNamespace

from views import PersistentView


def _styles(team_a_name=None, team_b_name=None):
    added = []
    view = SimpleNamespace(
        team_a_name=team_a_name, team_b_name=team_b_name,
        team_assignment_callback=None, randomize_teams_callback=None,
        add_test_users_premade_callback=None, draft_session_id="s1",
        _add_button=lambda label, style, suffix, cb, **kw: added.append((suffix, label, style)))
    PersistentView._add_premade_buttons(view)
    return {suffix: (label, style) for suffix, label, style in added}


def test_team_a_button_is_red_and_team_b_button_is_blue():
    styles = _styles()
    assert styles["Team_A"] == ("Team Red", "red")
    assert styles["Team_B"] == ("Team Blue", "blurple")


def test_named_teams_keep_their_side_colours():
    styles = _styles("Pack Rats", "Mox Boys")
    assert styles["Team_A"][1] == "red" and styles["Team_B"][1] == "blurple"


def test_no_other_premade_button_shares_a_team_colour():
    styles = _styles()
    others = [style for suffix, (_, style) in styles.items() if suffix not in ("Team_A", "Team_B")]
    assert not {"red", "danger", "blurple", "primary"} & set(others)
