"""Smoke tests for cogs/tournament_commands.py (Slice 1)."""
from unittest.mock import MagicMock

from helpers.permissions import is_bot_manager


def test_cog_imports_and_setup_registers():
    from cogs.tournament_commands import TournamentCog, setup

    bot = MagicMock()
    setup(bot)
    bot.add_cog.assert_called_once()
    assert isinstance(bot.add_cog.call_args.args[0], TournamentCog)


def test_tournament_group_has_slice_one_and_two_commands():
    from cogs.tournament_commands import TournamentCog

    subcommands = {cmd.name for cmd in TournamentCog.tournament.subcommands}
    assert {"create", "register", "status",
            "start", "set_result", "next_round", "finish",
            "add_team", "remove_team", "add_match", "refresh_standings",
            "recover_draft"} <= subcommands


def test_admin_commands_are_gated_by_bot_manager_check():
    from cogs.tournament_commands import TournamentCog

    for command in ("create", "start", "set_result", "next_round", "finish",
                    "add_team", "remove_team", "add_match", "refresh_standings",
                    "recover_draft"):
        assert is_bot_manager in getattr(TournamentCog, command).checks, command


def test_register_and_status_are_open_to_everyone():
    from cogs.tournament_commands import TournamentCog

    assert is_bot_manager not in TournamentCog.register.checks
    assert is_bot_manager not in TournamentCog.status.checks


def test_recorded_result_line_formats_score():
    from cogs.tournament_commands import _recorded_result_line

    line = _recorded_result_line("Latecomers", "Strixhaven Dropouts", 5, 4)
    assert line == "✅ Result recorded: **Latecomers** 5–4 **Strixhaven Dropouts**"
