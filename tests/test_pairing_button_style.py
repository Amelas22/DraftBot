"""Unit tests for the pairing-button coloring helper."""
import discord

from views import pairing_button_style


class _MR:
    def __init__(self, winner_id):
        self.winner_id = winner_id


TEAM_A = ["1", "2", "3"]
TEAM_B = ["4", "5", "6"]


def test_team_a_winner_is_red():
    assert pairing_button_style(_MR("2"), TEAM_A, TEAM_B) == discord.ButtonStyle.danger


def test_team_b_winner_is_blurple():
    assert pairing_button_style(_MR("5"), TEAM_A, TEAM_B) == discord.ButtonStyle.primary


def test_unreported_is_grey():
    assert pairing_button_style(_MR(None), TEAM_A, TEAM_B) == discord.ButtonStyle.secondary


def test_winner_not_on_either_team_is_grey():
    assert pairing_button_style(_MR("99"), TEAM_A, TEAM_B) == discord.ButtonStyle.secondary
