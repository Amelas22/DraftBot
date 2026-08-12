"""The registration board: renders roster + payment state, and edits in place."""
import pytest

from models.tournament import Tournament, TournamentParticipant
from services.tournament_formatter import create_registration_embed


def _tournament(name="Fee Cup", fee=1, status="registration"):
    return Tournament(id=1, guild_id="g1", name=name, total_rounds=0, format="manual",
                      status=status, entry_fee=fee, payout_structure="winner_take_all")


def _team(pid, name, captain, status):
    return TournamentParticipant(id=pid, tournament_id=1, team_id=pid, team_name=name,
                                 captain_user_id=captain, status=status)


def _text(embed):
    return embed.description + "".join(f.name + f.value for f in embed.fields)


def test_paid_board_marks_paid_and_pending_with_a_deficit():
    teams = [_team(1, "Alpha", "10", "paid"), _team(2, "Gamma", "20", "pending")]
    embed = create_registration_embed(_tournament(), teams, pot=1, deficits={2: 1})
    text = _text(embed)

    assert "Registration open" in embed.title
    assert "Teams (1/2 paid)" in text
    assert "✅ **Alpha**" in text and "⏳ **Gamma**" in text
    assert "needs 1 more tix" in text and "/wallet deposit 1" in text
    assert "Entry fee:** 1 tix/team" in text and "Prize pool:** 1 tix" in text


def test_free_board_is_a_plain_roster():
    teams = [_team(1, "Alpha", "10", "paid")]
    embed = create_registration_embed(_tournament(fee=0), teams)
    text = _text(embed)

    assert "Teams (1)" in text
    assert "Entry fee" not in text and "Prize pool" not in text
    assert "needs" not in text and "⏳" not in text


def test_all_paid_board_shows_no_deficit_lines():
    teams = [_team(1, "Alpha", "10", "paid"), _team(2, "Beta", "20", "paid")]
    text = _text(create_registration_embed(_tournament(), teams, pot=2, deficits={}))

    assert "Teams (2/2 paid)" in text
    assert "needs" not in text and "⏳" not in text


def test_closed_board_drops_deficits_and_says_closed():
    teams = [_team(1, "Alpha", "10", "paid"), _team(2, "Gamma", "20", "pending")]
    embed = create_registration_embed(_tournament(), teams, pot=1, deficits={2: 1}, closed=True)

    assert "Registration closed" in embed.title
    assert "needs" not in _text(embed)


def test_empty_board_invites_registration():
    assert "/tournament register" in _text(create_registration_embed(_tournament(), []))
