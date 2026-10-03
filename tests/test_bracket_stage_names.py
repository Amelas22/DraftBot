"""What a bracket round is called, wherever it is named."""
from types import SimpleNamespace

from models.tournament import STAGE_PLAY_IN, STAGE_PLAYOFF, STAGE_SWISS
from services.tournament_formatter import bracket_stage_name, round_label
from services.tournament_service import is_playoff


def test_bracket_rounds_are_named_by_how_many_matches_they_hold():
    assert bracket_stage_name(STAGE_PLAYOFF, 1) == "Final"
    assert bracket_stage_name(STAGE_PLAYOFF, 2) == "Semifinal"
    assert bracket_stage_name(STAGE_PLAYOFF, 4) == "Quarterfinal"
    assert bracket_stage_name(STAGE_PLAYOFF, 8) == "Round of 16"


def test_the_play_in_is_named_for_what_it_is_not_its_size():
    assert bracket_stage_name(STAGE_PLAY_IN, 1) == "Play-in"


def test_round_label_uses_the_stage_name_when_it_knows_the_match_count():
    assert round_label(6, 8, STAGE_PLAYOFF, matches_in_round=2) == "Semifinal"
    assert round_label(6, 3, STAGE_SWISS, swiss_noun="Week", matches_in_round=20) == "Week 3"


def test_round_label_keeps_the_numbered_form_without_a_count():
    assert round_label(6, 8, STAGE_PLAYOFF) == "Playoff round 2"


def test_both_bracket_stages_are_playoff_rounds():
    assert is_playoff(SimpleNamespace(stage=STAGE_PLAY_IN))
    assert is_playoff(SimpleNamespace(stage=STAGE_PLAYOFF))
    assert not is_playoff(SimpleNamespace(stage=STAGE_SWISS))
