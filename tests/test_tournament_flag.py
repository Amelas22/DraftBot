"""Tests for tournament feature-flag semantics: on by default, explicit opt-out,
and the admin enable/disable commands."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config as config_module
from cogs.tournament_commands import tournament_enabled


# ---- default-on semantics ----------------------------------------------------------

def test_tournament_enabled_defaults_true_when_key_missing():
    with patch("cogs.tournament_commands.get_config", return_value={"features": {}}):
        assert tournament_enabled(123) is True


def test_tournament_enabled_respects_explicit_false():
    cfg = {"features": {"tournament": False}}
    with patch("cogs.tournament_commands.get_config", return_value=cfg):
        assert tournament_enabled(123) is False


def test_tournament_enabled_true_without_features_block():
    with patch("cogs.tournament_commands.get_config", return_value={}):
        assert tournament_enabled(123) is True


# ---- migrate_configs injects the key -------------------------------------------------

def _migrate_fake_guild(features):
    guild_id = "999000999000999000"
    config_module.bot_config.configs[guild_id] = {"features": dict(features)}
    try:
        with patch.object(config_module.bot_config, "save_config"):
            config_module.migrate_configs()
        return config_module.bot_config.configs[guild_id]["features"]
    finally:
        del config_module.bot_config.configs[guild_id]


def test_migrate_configs_adds_tournament_true_when_missing():
    features = _migrate_fake_guild({"winston_draft": False})
    assert features["tournament"] is True


def test_migrate_configs_keeps_explicit_false():
    features = _migrate_fake_guild({"tournament": False})
    assert features["tournament"] is False


# ---- enable/disable commands ------------------------------------------------------------

def test_enable_and_disable_commands_exist_and_are_admin_gated():
    from helpers.permissions import is_bot_manager
    from cogs.tournament_commands import TournamentCog

    subcommands = {cmd.name for cmd in TournamentCog.tournament.subcommands}
    assert {"enable", "disable"} <= subcommands
    assert is_bot_manager in TournamentCog.enable.checks
    assert is_bot_manager in TournamentCog.disable.checks


@pytest.mark.asyncio
async def test_enable_command_works_while_feature_is_disabled():
    """Enable must not be blocked by the very flag it manages."""
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    ctx = MagicMock()
    ctx.guild.id = 123
    ctx.respond = AsyncMock()

    with patch("cogs.tournament_commands.update_setting", return_value=True) as updater, \
         patch("cogs.tournament_commands.get_config", return_value={"features": {"tournament": False}}):
        await TournamentCog.enable.callback(cog, ctx)

    updater.assert_called_once_with(123, "features.tournament", True)
    msg = ctx.respond.call_args.args[0]
    assert "enabled" in msg.lower()


@pytest.mark.asyncio
async def test_disable_command_turns_flag_off():
    from cogs.tournament_commands import TournamentCog

    cog = TournamentCog(MagicMock())
    ctx = MagicMock()
    ctx.guild.id = 123
    ctx.respond = AsyncMock()

    with patch("cogs.tournament_commands.update_setting", return_value=True) as updater:
        await TournamentCog.disable.callback(cog, ctx)

    updater.assert_called_once_with(123, "features.tournament", False)
    msg = ctx.respond.call_args.args[0]
    assert "disabled" in msg.lower()
