"""Tests for bots_with_draft_access role config: default, opt-out, and migration."""
from unittest.mock import patch

import config as config_module
from config import DEFAULT_BOTS_WITH_DRAFT_ACCESS, get_bots_with_draft_access


# ---- getter semantics ----------------------------------------------------------------

def test_defaults_to_scryfall_when_key_missing():
    with patch("config.get_config", return_value={"roles": {}}):
        assert get_bots_with_draft_access(123) == DEFAULT_BOTS_WITH_DRAFT_ACCESS


def test_defaults_apply_without_roles_block():
    with patch("config.get_config", return_value={}):
        assert get_bots_with_draft_access(123) == DEFAULT_BOTS_WITH_DRAFT_ACCESS


def test_explicit_empty_list_opts_out():
    with patch("config.get_config", return_value={"roles": {"bots_with_draft_access": []}}):
        assert get_bots_with_draft_access(123) == []


def test_default_is_not_aliased_to_caller():
    with patch("config.get_config", return_value={"roles": {}}):
        get_bots_with_draft_access(123).append("Mutated")
        assert get_bots_with_draft_access(123) == DEFAULT_BOTS_WITH_DRAFT_ACCESS


# ---- migrate_configs injects the key ---------------------------------------------------

def _migrate_fake_guild(roles):
    guild_id = "999000999000999000"
    config_module.bot_config.configs[guild_id] = {"roles": dict(roles)}
    try:
        with patch.object(config_module.bot_config, "save_config"):
            config_module.migrate_configs()
        return config_module.bot_config.configs[guild_id]["roles"]
    finally:
        del config_module.bot_config.configs[guild_id]


def test_migrate_configs_adds_bots_with_draft_access_when_missing():
    roles = _migrate_fake_guild({"admin": "Cube Overseer"})
    assert roles["bots_with_draft_access"] == DEFAULT_BOTS_WITH_DRAFT_ACCESS
    assert roles["bots_with_draft_access"] is not DEFAULT_BOTS_WITH_DRAFT_ACCESS


def test_migrate_configs_keeps_explicit_opt_out():
    roles = _migrate_fake_guild({"bots_with_draft_access": []})
    assert roles["bots_with_draft_access"] == []
