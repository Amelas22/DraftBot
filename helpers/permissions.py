"""Shared permission decorators for Discord bot commands."""
from discord.ext import commands

ADMIN_ROLE_NAME = "Bot Manager"


def has_bot_manager_role():
    """Check if user is the bot owner OR has the Bot Manager role."""
    async def predicate(ctx):
        if await ctx.bot.is_owner(ctx.author):
            return True
        return any(role.name == ADMIN_ROLE_NAME for role in ctx.author.roles)
    return commands.check(predicate)
