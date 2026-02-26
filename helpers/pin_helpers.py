import discord
from loguru import logger


async def safe_pin(message, _logger=None):
    """Pin a message, logging a warning on failure instead of raising."""
    _log = _logger or logger
    try:
        await message.pin()
    except discord.errors.Forbidden:
        _log.warning("Missing permissions to pin message {} in channel {}", message.id, message.channel.id)
    except discord.errors.HTTPException as e:
        _log.warning("Failed to pin message {} in channel {}: {}", message.id, message.channel.id, e)


async def safe_unpin(message, _logger=None):
    """Unpin a message, logging a warning on failure instead of raising."""
    _log = _logger or logger
    try:
        await message.unpin()
    except discord.errors.Forbidden:
        _log.warning("Missing permissions to unpin message {} in channel {}", message.id, message.channel.id)
    except discord.errors.HTTPException as e:
        _log.warning("Failed to unpin message {} in channel {}: {}", message.id, message.channel.id, e)
