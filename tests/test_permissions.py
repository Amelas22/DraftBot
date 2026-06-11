import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from discord.ext import commands

from helpers import permissions
from helpers.permissions import (
    get_manager_role_names,
    is_bot_manager,
    handle_application_command_error,
    ADMIN_ROLE_NAME,
)


def make_ctx(role_names=(), is_owner=False, guild_id=123, guild=True, manage_roles=False):
    ctx = MagicMock()
    ctx.bot.is_owner = AsyncMock(return_value=is_owner)
    ctx.author.roles = [MagicMock(name=f"role-{n}") for n in role_names]
    # MagicMock(name=...) sets the mock's repr name, not .name attribute; set explicitly
    for role, n in zip(ctx.author.roles, role_names):
        role.name = n
    ctx.author.guild_permissions.manage_roles = manage_roles
    if guild:
        ctx.guild.id = guild_id
    else:
        ctx.guild = None
    return ctx


# ---- get_manager_role_names -------------------------------------------------

def test_defaults_include_bot_manager_and_bot_lord():
    with patch("helpers.permissions.get_config", return_value={}):
        names = get_manager_role_names(123)
    assert "Bot Manager" in names
    assert "Bot Lord" in names


def test_configured_string_role_is_accepted():
    with patch("helpers.permissions.get_config", return_value={"roles": {"bot_manager": "Council"}}):
        names = get_manager_role_names(123)
    assert "Council" in names
    assert "Bot Manager" in names  # defaults still apply


def test_configured_list_of_roles_is_accepted():
    cfg = {"roles": {"bot_manager": ["Council", "Overlord"]}}
    with patch("helpers.permissions.get_config", return_value=cfg):
        names = get_manager_role_names(123)
    assert {"Council", "Overlord"}.issubset(names)


def test_missing_config_falls_back_to_defaults():
    with patch("helpers.permissions.get_config", side_effect=Exception("boom")):
        names = get_manager_role_names(123)
    assert "Bot Manager" in names and "Bot Lord" in names


# ---- is_bot_manager ---------------------------------------------------------

@pytest.mark.asyncio
async def test_owner_is_always_allowed():
    ctx = make_ctx(role_names=(), is_owner=True)
    assert await is_bot_manager(ctx) is True


@pytest.mark.asyncio
async def test_user_with_bot_lord_role_is_allowed():
    ctx = make_ctx(role_names=("Bot Lord",))
    with patch("helpers.permissions.get_config", return_value={}):
        assert await is_bot_manager(ctx) is True


@pytest.mark.asyncio
async def test_user_with_bot_manager_role_is_allowed():
    ctx = make_ctx(role_names=("Bot Manager",))
    with patch("helpers.permissions.get_config", return_value={}):
        assert await is_bot_manager(ctx) is True


@pytest.mark.asyncio
async def test_user_with_configured_role_is_allowed():
    ctx = make_ctx(role_names=("Council",))
    with patch("helpers.permissions.get_config", return_value={"roles": {"bot_manager": "Council"}}):
        assert await is_bot_manager(ctx) is True


@pytest.mark.asyncio
async def test_user_without_any_manager_role_is_denied():
    ctx = make_ctx(role_names=("Admin", "Cube Drafter"))
    with patch("helpers.permissions.get_config", return_value={}):
        assert await is_bot_manager(ctx) is False


@pytest.mark.asyncio
async def test_user_with_manage_roles_permission_is_allowed():
    ctx = make_ctx(role_names=(), manage_roles=True)
    with patch("helpers.permissions.get_config", return_value={}):
        assert await is_bot_manager(ctx) is True


@pytest.mark.asyncio
async def test_author_without_guild_permissions_attribute_is_denied():
    # e.g. a plain discord.User (DM-like contexts) has no guild_permissions
    ctx = make_ctx(role_names=())
    ctx.author.guild_permissions = None
    with patch("helpers.permissions.get_config", return_value={}):
        assert await is_bot_manager(ctx) is False


# ---- quiz scheduling commands use the unified check --------------------------

def test_quiz_scheduling_commands_use_bot_manager_check():
    from cogs.quiz_scheduling_cog import QuizSchedulingCog

    command_names = (
        "setup_quiz_channel",
        "add_quiz_schedule",
        "list_quiz_schedules",
        "remove_quiz_schedule",
        "edit_quiz_timezone",
        "enable_quiz_posting",
        "disable_quiz_posting",
    )
    for name in command_names:
        cmd = getattr(QuizSchedulingCog, name)
        assert is_bot_manager in cmd.checks, (
            f"/{name} should use the unified bot-manager check"
        )


# ---- handle_application_command_error ---------------------------------------

@pytest.mark.asyncio
async def test_check_failure_sends_clear_permission_message():
    ctx = make_ctx(role_names=())
    ctx.interaction.response.is_done.return_value = False
    ctx.respond = AsyncMock()
    with patch("helpers.permissions.get_config", return_value={}):
        await handle_application_command_error(ctx, commands.CheckFailure("nope"))
    ctx.respond.assert_called_once()
    msg = ctx.respond.call_args.args[0]
    assert "permission" in msg.lower()
    assert "Bot Lord" in msg  # lists accepted roles
    assert "Manage Roles" in msg  # mentions the equivalent Discord permission
    assert ctx.respond.call_args.kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_not_owner_failure_names_the_owner_requirement():
    ctx = make_ctx(role_names=())
    ctx.interaction.response.is_done.return_value = False
    ctx.respond = AsyncMock()
    await handle_application_command_error(ctx, commands.NotOwner())
    ctx.respond.assert_called_once()
    msg = ctx.respond.call_args.args[0]
    assert "owner" in msg.lower()
    assert "roles" not in msg.lower()  # not the misleading role message


@pytest.mark.asyncio
async def test_generic_error_sends_generic_message_and_does_not_raise():
    ctx = make_ctx(role_names=())
    ctx.interaction.response.is_done.return_value = False
    ctx.respond = AsyncMock()
    await handle_application_command_error(ctx, RuntimeError("kaboom"))
    ctx.respond.assert_called_once()
    msg = ctx.respond.call_args.args[0]
    assert "wrong" in msg.lower() or "error" in msg.lower()


@pytest.mark.asyncio
async def test_uses_followup_when_already_responded():
    ctx = make_ctx(role_names=())
    ctx.interaction.response.is_done.return_value = True
    ctx.respond = AsyncMock()
    ctx.followup.send = AsyncMock()
    with patch("helpers.permissions.get_config", return_value={}):
        await handle_application_command_error(ctx, commands.CheckFailure("nope"))
    ctx.followup.send.assert_called_once()
    ctx.respond.assert_not_called()
