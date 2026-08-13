"""Where a tournament's messages live.

Standings and pairings want different homes: standings is one message edited in
place all season, so it belongs somewhere quiet and findable, while pairings are
a fresh burst of clickable messages every round. Posting both wherever the
command happened to be typed buries the former under the latter.

Both channels are stored as ids under the guild config's ``tournament`` section
rather than by name, so renaming a channel is a non-event (the name-lookup
convention used elsewhere in this repo would silently create a duplicate and
orphan the live standings message).
"""
import discord
from loguru import logger

# Config keys under the guild config's "tournament" section.
STANDINGS_CHANNEL_SETTING = "standings_channel_id"
PLAY_CHANNEL_SETTING = "play_channel_id"

# Name used when the bot has to create the standings channel itself.
STANDINGS_CHANNEL_NAME = "tournament-standings"

STANDINGS_CHANNEL_TOPIC = "Live tournament standings — updated automatically after every reported match."


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


def _standings_overwrites(guild):
    """Players read the standings; only the bot writes them."""
    return {
        guild.default_role: discord.PermissionOverwrite(send_messages=False, read_messages=True),
        guild.me: discord.PermissionOverwrite(send_messages=True, read_messages=True, embed_links=True),
    }


async def ensure_standings_channel(guild, config, chosen):
    """Return (channel, created) for the standings channel.

    Preference order: an explicitly chosen channel, the configured one, an
    existing channel already carrying the default name, and only then a new
    read-only channel in the draft category. Adopting a same-named channel
    before creating keeps a re-run from producing #tournament-standings-2.

    An existing channel gets its bot permissions repaired — a standings channel
    the bot cannot post in is worse than not having one, and the same drift
    check guards the live-drafts channel (livedrafts.py).
    """
    channel = chosen or resolve_channel(guild, config, STANDINGS_CHANNEL_SETTING)
    if channel is None:
        channel = discord.utils.get(guild.text_channels, name=STANDINGS_CHANNEL_NAME)

    if channel is not None:
        perms = channel.overwrites_for(guild.me)
        if not perms.send_messages or not perms.embed_links:
            await channel.set_permissions(
                guild.me, send_messages=True, read_messages=True, embed_links=True)
            logger.info(f"Repaired bot permissions on standings channel {channel.id}")
        return channel, False

    category_name = config.get("categories", {}).get("draft_name")
    category = discord.utils.get(guild.categories, name=category_name) if category_name else None
    channel = await guild.create_text_channel(
        name=STANDINGS_CHANNEL_NAME,
        category=category,
        topic=STANDINGS_CHANNEL_TOPIC,
        overwrites=_standings_overwrites(guild),
    )
    logger.info(f"Created standings channel {channel.id} in guild {guild.id}")
    return channel, True
