"""Shared permission decorators for Discord bot commands."""
from discord.ext import commands
from loguru import logger

from config import get_config

# Primary role name. This is the role the bot creates via /setup and refers to
# in user-facing messages. It is always accepted.
ADMIN_ROLE_NAME = "Bot Manager"

# Role names that are accepted on every guild without any configuration.
DEFAULT_MANAGER_ROLE_NAMES = (ADMIN_ROLE_NAME, "Bot Lord")


def get_manager_role_names(guild_id):
    """Return the set of role names that grant bot-manager access for a guild.

    Always includes the built-in defaults (``Bot Manager``, ``Bot Lord``). A
    guild may extend this by setting ``roles.bot_manager`` in its config to a
    string or a list of strings.
    """
    names = set(DEFAULT_MANAGER_ROLE_NAMES)
    try:
        configured = get_config(guild_id).get("roles", {}).get("bot_manager")
    except Exception:
        configured = None
    if isinstance(configured, str):
        names.add(configured)
    elif isinstance(configured, (list, tuple)):
        names.update(c for c in configured if isinstance(c, str))
    return names


async def is_bot_manager(ctx):
    """True if the invoker is the bot owner, has an accepted manager role, or
    has the Manage Roles permission in the guild."""
    if await ctx.bot.is_owner(ctx.author):
        return True
    guild = getattr(ctx, "guild", None)
    if guild is None:
        return False
    allowed = get_manager_role_names(guild.id)
    if any(role.name in allowed for role in ctx.author.roles):
        return True
    perms = getattr(ctx.author, "guild_permissions", None)
    return bool(perms and perms.manage_roles)


def has_bot_manager_role():
    """Check if user is the bot owner OR has an accepted Bot Manager role."""
    return commands.check(is_bot_manager)


async def _send_error(ctx, message):
    """Send an ephemeral message to the user, whether or not the interaction
    has already been acknowledged."""
    try:
        if ctx.interaction.response.is_done():
            await ctx.followup.send(message, ephemeral=True)
        else:
            await ctx.respond(message, ephemeral=True)
    except Exception:
        logger.exception("Failed to send command error message to user")


async def handle_application_command_error(ctx, error):
    """Global application command error handler.

    Turns a failed permission check into a clear ephemeral message instead of
    leaving the interaction unacknowledged (which Discord surfaces as
    "This application does not respond").
    """
    original = getattr(error, "original", error)
    if isinstance(error, commands.NotOwner) or isinstance(original, commands.NotOwner):
        await _send_error(ctx, "❌ Only the bot owner can use this command.")
        return
    if isinstance(error, commands.CheckFailure) or isinstance(original, commands.CheckFailure):
        guild = getattr(ctx, "guild", None)
        if guild is not None:
            roles = " / ".join(sorted(get_manager_role_names(guild.id)))
        else:
            roles = ADMIN_ROLE_NAME
        await _send_error(
            ctx,
            f"❌ You don't have permission to use this command. "
            f"You need one of these roles: {roles}, or the Manage Roles permission.",
        )
        return

    logger.error(f"Unhandled application command error: {original!r}")
    await _send_error(ctx, "❌ Something went wrong running that command. Please try again.")
