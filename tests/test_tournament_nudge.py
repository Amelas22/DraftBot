"""Tests for tournament_nudge.py view construction."""
from services.tournament_linking import CandidateLink
from tournament_nudge import (
    TournamentLinkButtonView,
    TournamentLinkSelectView,
    build_nudge_view,
)


def _cand(match_id, a="Latecomers", b="Strixhaven Dropouts", rnd=2, conf=0.9):
    return CandidateLink(match_id=match_id, reversed=False, confidence=conf,
                         a_name=a, b_name=b, round_number=rnd)


def test_no_candidates_returns_none():
    assert build_nudge_view("s1", []) is None


def test_single_candidate_builds_button_view():
    content, view = build_nudge_view("s1", [_cand(10)])
    assert isinstance(view, TournamentLinkButtonView)
    assert "Latecomers" in content and "Strixhaven Dropouts" in content
    button = view.children[0]
    assert button.custom_id == "tourney_link:s1:10"


def test_multiple_candidates_build_select_view():
    content, view = build_nudge_view("s1", [_cand(10), _cand(11, a="Rakdos Intolerant",
                                                             b="European Juggernauts", rnd=3)])
    assert isinstance(view, TournamentLinkSelectView)
    select = view.children[0]
    assert select.custom_id == "tourney_link_select:s1"
    values = {opt.value for opt in select.options}
    assert values == {"10", "11"}
