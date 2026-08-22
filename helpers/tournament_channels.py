"""Where a tournament's messages live.

Standings and pairings want different homes: standings is one message edited in
place all season, so it belongs somewhere quiet and findable, while pairings are
a fresh burst of clickable messages every round. Posting both wherever the
command happened to be typed buries the former under the latter.

Both channels are created in the category of the channel /tournament
setup_channels was run from — a league or tournament category is where an
organiser already keeps this stuff, so the invocation site says where the season
lives without another setting to configure.

Both are stored as ids under the guild config's ``tournament`` section rather
than by name, so renaming a channel is a non-event (the name-lookup convention
used elsewhere in this repo would silently create a duplicate and orphan the
live standings message).
"""
from typing import NamedTuple

import discord
from loguru import logger


class ChannelSpec(NamedTuple):
    """One kind of tournament channel: what to call it, and who may talk in it."""

    setting: str      # config key under the guild config's "tournament" section
    name: str         # name used when the bot creates it
    topic: str
    read_only: bool   # True: only the bot posts (standings)


# "default_" keeps these distinct from Tournament.standings_channel_id, which is
# a different fact: the column records where one event's message actually
# landed, these say where the next one should go.
STANDINGS = ChannelSpec(
    setting="default_standings_channel_id",
    name="tournament-standings",
    topic="Live tournament standings — updated automatically after every reported match.",
    read_only=True,
)
PAIRINGS = ChannelSpec(
    setting="default_play_channel_id",
    name="tournament-pairings",
    topic="Weekly pairings — each match gets its own thread; start the draft in there.",
    # Players talk in the match threads that hang off this channel's lines.
    read_only=False,
)


def resolve_channel(guild, config, setting):
    """The configured channel for ``setting``, or None.

    None covers both "never configured" and "configured but since deleted";
    callers fall back to wherever the command was run rather than failing.
    """
    channel_id = config.get("tournament", {}).get(setting)
    if not channel_id:
        return None
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        logger.warning(f"Configured tournament {setting} {channel_id} no longer exists in guild {guild.id}")
    return channel


def _overwrites(guild, spec):
    """Read-only channels let only the bot post; the rest keep guild defaults.

    Empty dict, never None: create_text_channel validates the argument with
    ``isinstance(overwrites, dict)`` and rejects None outright.
    """
    if not spec.read_only:
        return {}
    return {
        guild.default_role: discord.PermissionOverwrite(send_messages=False, read_messages=True),
        guild.me: discord.PermissionOverwrite(send_messages=True, read_messages=True, embed_links=True),
    }


async def ensure_channel(guild, config, spec, category, chosen=None):
    """Return (channel, created) for one kind of tournament channel.

    Preference order: an explicitly chosen channel, the configured one, an
    existing channel already carrying the default name, and only then a new one
    in ``category``. Adopting a same-named channel before creating keeps a
    re-run from producing #tournament-standings-2.

    An adopted channel gets its bot permissions repaired — a channel the bot
    cannot post in is worse than not having one, and the same drift check
    guards the live-drafts channel (livedrafts.py).
    """
    channel = (
        chosen
        or resolve_channel(guild, config, spec.setting)
        or discord.utils.get(guild.text_channels, name=spec.name)
    )
    if channel is not None:
        perms = channel.overwrites_for(guild.me)
        if not perms.send_messages or not perms.embed_links:
            await channel.set_permissions(
                guild.me, send_messages=True, read_messages=True, embed_links=True)
            logger.info(f"Repaired bot permissions on {spec.name} channel {channel.id}")
        return channel, False

    channel = await guild.create_text_channel(
        name=spec.name,
        category=category,
        topic=spec.topic,
        overwrites=_overwrites(guild, spec),
    )
    logger.info(
        f"Created {spec.name} channel {channel.id} in guild {guild.id} "
        f"(category {category.id if category else 'none'})")
    return channel, True
