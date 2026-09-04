"""One row per side, instead of four dispatch tables on the same key.

Four modules each looked up a channel-name prefix to get a different fact --
which rooms a sub should be granted, which channel holds a team's pools, whose
roster to rebuild, who the opponents are. Each carried its own copy of the
mapping, so "the A side is the red one" was asserted in four places and agreed
by convention.

A Side row states it once. These pin the row, and the two dispatch helpers that
read it.
"""
from types import SimpleNamespace

import pytest

from helpers.draft_rooms import (BLUE_SIDE, RED_SIDE, SHARED_SIDE, SIDES,
                                 side_by_key, side_by_prefix)
from helpers.team_names import BLUE, RED


def _draft(team_a=("a1", "a2"), team_b=("b1", "b2"), sign_ups=None,
           team_a_name=None, team_b_name=None):
    return SimpleNamespace(
        team_a=list(team_a), team_b=list(team_b),
        sign_ups=sign_ups if sign_ups is not None else {"a1": "A1", "b1": "B1"},
        team_a_name=team_a_name, team_b_name=team_b_name)


def test_each_team_side_agrees_with_itself():
    """The colour, the room prefix, the roster and the pools column are one
    fact about a side. Stating them in one row is the point -- a row that
    disagreed with itself would put a sub in the other team's rooms."""
    draft = _draft()

    assert RED_SIDE.label is RED
    assert RED_SIDE.prefix.lower().startswith(RED.color)
    assert RED_SIDE.roster(draft) == ["a1", "a2"]
    assert RED_SIDE.pools_column == "team_a_pools_destination_id"

    assert BLUE_SIDE.label is BLUE
    assert BLUE_SIDE.prefix.lower().startswith(BLUE.color)
    assert BLUE_SIDE.roster(draft) == ["b1", "b2"]
    assert BLUE_SIDE.pools_column == "team_b_pools_destination_id"


def test_the_two_team_sides_are_each_other_s_opponents():
    """Own roster and opponents come from the same row, which is what makes it
    impossible to tag one team while scouting the other."""
    draft = _draft()

    a, b = draft.team_a, draft.team_b
    assert RED_SIDE.roster(draft) == BLUE_SIDE.opponents_of(a, b)
    assert BLUE_SIDE.roster(draft) == RED_SIDE.opponents_of(a, b)


def test_the_shared_chat_is_a_side_with_no_opponent():
    """The draft chat holds both teams, so nobody in it is an opponent -- and
    its roster is everybody, which is what keeps a team-less draft (swiss) from
    rebuilding its only room with no members in it."""
    draft = _draft()

    assert SHARED_SIDE.opponents_of(draft.team_a, draft.team_b) == []
    assert set(SHARED_SIDE.roster(draft)) == {"a1", "a2", "b1", "b2"}
    assert SHARED_SIDE.pools_column == "open_pools_destination_id"


def test_a_team_less_draft_falls_back_to_its_sign_ups():
    """Swiss has no team_a/team_b at all. Falling back to team_a + team_b would
    give an empty roster and lock the whole draft out of its only channel."""
    draft = _draft(team_a=(), team_b=(), sign_ups={"p1": "One", "p2": "Two"})

    assert set(SHARED_SIDE.roster(draft)) == {"p1", "p2"}


def test_the_shared_chat_announces_itself_as_the_draft_not_a_colour():
    """It has no side, so it has no colour. "this draft" is what the sub-grant
    confirmation has always called it."""
    assert SHARED_SIDE.named(_draft()).name == "this draft"
    assert SHARED_SIDE.label.emoji == ""


def test_a_room_is_matched_to_its_side_by_prefix():
    """The room name is still what says which side owns it; what changed is
    that only one module knows the pattern.

    Case-insensitive because Discord lowercases text channel names on creation
    but leaves voice names as sent.
    """
    assert RED_SIDE.claims_room("Red-Team-Chat-icebind-pillar-36")
    assert BLUE_SIDE.claims_room("blue-team-voice-icebind-pillar-36")
    assert not RED_SIDE.claims_room("Blue-Team-Chat-icebind-pillar-36")
    assert not RED_SIDE.claims_room("general")


def test_a_prefix_resolves_to_the_side_that_owns_it():
    """What /regenerate_rooms' stored choice value and the sub grant both do
    with the prefix they are handed."""
    assert side_by_prefix("Red-Team") is RED_SIDE
    assert side_by_prefix("Blue-Team") is BLUE_SIDE
    assert side_by_prefix("Draft") is SHARED_SIDE
    assert side_by_prefix("nonsense") is None


def test_the_add_sub_choice_value_selects_a_side():
    """/add_sub's stored values are "A"/"B"; this is the one place that turns
    one into the side it names."""
    assert side_by_key("A") is RED_SIDE
    assert side_by_key("B") is BLUE_SIDE
    assert side_by_key(None) is None


def test_a_named_team_keeps_its_name_and_an_unnamed_one_takes_the_colour():
    """The label still comes from team_labels, so a side agrees with every
    other surface about what it is called."""
    unnamed = _draft()
    named = _draft(team_a_name="Pack Rats", team_b_name="PDX Pandas")

    assert RED_SIDE.named(unnamed).name == RED.name
    assert RED_SIDE.named(named).name == "Pack Rats"
    assert BLUE_SIDE.named(named).name == "PDX Pandas"


@pytest.mark.parametrize("side, expected", [
    (RED_SIDE, {"red-team-chat-icebind-pillar-36", "red-team-voice-icebind-pillar-36"}),
    (BLUE_SIDE, {"blue-team-chat-icebind-pillar-36", "blue-team-voice-icebind-pillar-36"}),
])
def test_a_side_names_both_of_its_rooms(side, expected):
    """A sub gets the text room and the voice room; missing the voice one
    leaves them able to read the team but not talk to it.

    Spelled out rather than rebuilt from side.prefix: an expectation assembled
    the same way the implementation assembles it passes for any consistent but
    wrong convention, and these names are matched against live Discord
    channels, so the convention IS the contract.
    """
    assert side.room_names("icebind-pillar-36") == expected
