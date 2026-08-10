"""Tests for stakes.debt_warning_threshold config: default, opt-out, migration."""
from unittest.mock import patch

import config as config_module
from config import DEFAULT_DEBT_WARNING_THRESHOLD, get_debt_warning_threshold


def test_defaults_to_100_when_key_missing():
    with patch("config.get_config", return_value={"stakes": {}}):
        assert get_debt_warning_threshold(123) == DEFAULT_DEBT_WARNING_THRESHOLD == 100


def test_defaults_apply_without_stakes_block():
    with patch("config.get_config", return_value={}):
        assert get_debt_warning_threshold(123) == 100


def test_explicit_zero_disables():
    with patch("config.get_config", return_value={"stakes": {"debt_warning_threshold": 0}}):
        assert get_debt_warning_threshold(123) == 0


def _migrate_fake_guild(stakes):
    guild_id = "999000999000999000"
    config_module.bot_config.configs[guild_id] = {"stakes": dict(stakes)}
    try:
        with patch.object(config_module.bot_config, "save_config"):
            config_module.migrate_configs()
        return config_module.bot_config.configs[guild_id]["stakes"]
    finally:
        del config_module.bot_config.configs[guild_id]


def test_migrate_configs_adds_threshold_when_missing():
    stakes = _migrate_fake_guild({"stake_multiple": 10})
    assert stakes["debt_warning_threshold"] == 100


def test_migrate_configs_keeps_explicit_zero():
    stakes = _migrate_fake_guild({"debt_warning_threshold": 0})
    assert stakes["debt_warning_threshold"] == 0


def test_migration_rebases_old_default_50_to_100():
    stakes = _migrate_fake_guild({"debt_warning_threshold": 50})
    assert stakes["debt_warning_threshold"] == 100


def test_migration_preserves_deliberate_values():
    assert _migrate_fake_guild({"debt_warning_threshold": 75})["debt_warning_threshold"] == 75
    assert _migrate_fake_guild({"debt_warning_threshold": 0})["debt_warning_threshold"] == 0
