"""The rooms one draft gets: their names, their permissions, and their creation.

Extracted from views.PersistentView.create_team_channel, which had grown to do
config lookup, category resolution, permission construction, channel creation,
database persistence and scouting-thread spawning in one body. The consequence
was that every change to how a room is made had to edit that body, so successive
changes kept re-opening the same twenty lines and could not be reviewed apart.

The seam is "make one room" versus "make this draft's rooms". Everything here is
the former: it takes a guild and plain values, returns a channel, and touches no
session state and no database. create_team_channel keeps the latter -- the
accumulation of channel_ids, the commit, the threads.

Nothing in this module changes behaviour relative to the code it came from. It
is a move, so that the changes that follow it are small.
"""
from typing import Any, Iterable

import discord
from loguru import logger

# The shared draft chat is addressed by this team name. It is not a team: it has
# no roster of its own, it is the one channel every player can see, and several
# rules key off it (admin access, no scouting threads, no team voice).
SHARED_CHAT_TEAM = "Draft"


def team_channel_name(team_name: str, friendly_id: str) -> str:
    """The text channel's name. Derived purely from the team and the draft, which
    is what lets a later run recognise a room it already made."""
    return f"{team_name}-Chat-{friendly_id}"


def team_voice_name(team_name: str, friendly_id: str) -> str:
    """The voice channel's name, paired with the text channel's above."""
    return f"{team_name}-Voice-{friendly_id}"


def resolve_category(guild: Any, config: dict[str, Any], key: str) -> Any:
    """The configured category under `key`, or None when the guild has not named
    one (or names one that no longer exists). None means "no category", which
    Discord accepts -- the channel is simply not grouped."""
    name = (config.get("categories") or {}).get(key)
    return discord.utils.get(guild.categories, name=name) if name else None


def team_overwrites(
    guild: Any,
    config: dict[str, Any],
    team_name: str,
    team_members: Iterable[Any],
    bot_role_names: Iterable[str],
) -> "dict[Any, discord.PermissionOverwrite]":
    """Who can see and speak in one draft room.

    Starts closed -- @everyone denied -- and opens it to the bot, the draft-access
    bots, this team's members, and (for the shared chat only) the admin role.

    Only bot-MANAGED integration roles are honoured for `bot_role_names`: the role
    Discord creates when a bot is invited cannot be assigned to a human, so a
    same-named vanity role cannot be used to read private team channels.
    """
    # Keyed by Role or Member, which have no common protocol to name -- both are
    # hashable and both are valid overwrite targets, which is all this needs.
    overwrites: "dict[Any, discord.PermissionOverwrite]" = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, manage_messages=True),
    }

    wanted = set(bot_role_names)
    bot_roles = [r for r in guild.roles if r.name in wanted and r.tags and r.tags.bot_id]
    for role in bot_roles:
        overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    logger.info(f"Draft-access bot roles for '{team_name}': {[r.name for r in bot_roles] or 'none'}")

    # The admin role sees the shared chat and NOT the private team channels --
    # a team's own room is private from staff by design.
    if team_name == SHARED_CHAT_TEAM:
        admin_role_name = (config.get("roles") or {}).get("admin")
        admin_role = discord.utils.get(guild.roles, name=admin_role_name) if admin_role_name else None
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(
                read_messages=True, manage_messages=True)
        else:
            logger.warning(f"Admin role '{admin_role_name}' not found in guild {guild.name}")

    for member in team_members:
        overwrites[member] = discord.PermissionOverwrite(
            read_messages=True, manage_messages=True)
    return overwrites


async def ensure_channel(guild: Any, kind: str, name: str,
                         overwrites: "dict[Any, discord.PermissionOverwrite]",
                         category: Any) -> Any:
    """The draft's `kind` ("text" or "voice") channel called `name`.

    One room, made once. The single place a draft channel comes into existence,
    so that everything later changes about HOW -- surviving a full category,
    recognising one an earlier run already made -- has exactly one home.
    """
    create = guild.create_text_channel if kind == "text" else guild.create_voice_channel
    channel = await create(name=name, overwrites=overwrites, category=category)
    logger.info(
        f"✅ Created {kind} channel '{name}' (ID: {channel.id}) in category "
        f"'{getattr(category, 'name', None) or 'None'}'"
    )
    return channel
