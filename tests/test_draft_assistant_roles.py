"""Tests for draft-assistant role config: default, opt-out, and migration."""
from unittest.mock import patch

import config as config_module
from config import DEFAULT_DRAFT_ASSISTANT_ROLES, get_draft_assistant_roles


# ---- getter semantics ----------------------------------------------------------------

def test_defaults_to_scryfall_when_key_missing():
    with patch("config.get_config", return_value={"roles": {}}):
        assert get_draft_assistant_roles(123) == DEFAULT_DRAFT_ASSISTANT_ROLES


def test_defaults_apply_without_roles_block():
    with patch("config.get_config", return_value={}):
        assert get_draft_assistant_roles(123) == DEFAULT_DRAFT_ASSISTANT_ROLES


def test_explicit_empty_list_opts_out():
    with patch("config.get_config", return_value={"roles": {"draft_assistants": []}}):
        assert get_draft_assistant_roles(123) == []


def test_default_is_not_aliased_to_caller():
    with patch("config.get_config", return_value={"roles": {}}):
        get_draft_assistant_roles(123).append("Mutated")
        assert get_draft_assistant_roles(123) == DEFAULT_DRAFT_ASSISTANT_ROLES


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


def test_migrate_configs_adds_draft_assistants_when_missing():
    roles = _migrate_fake_guild({"admin": "Cube Overseer"})
    assert roles["draft_assistants"] == DEFAULT_DRAFT_ASSISTANT_ROLES
    assert roles["draft_assistants"] is not DEFAULT_DRAFT_ASSISTANT_ROLES


def test_migrate_configs_keeps_explicit_opt_out():
    roles = _migrate_fake_guild({"draft_assistants": []})
    assert roles["draft_assistants"] == []
