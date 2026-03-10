"""
Test-only commands. This cog only loads when TEST_MODE_ENABLED is True in config.py.
"""
import asyncio
import discord
from discord.ext import commands
from loguru import logger

from config import TEST_MODE_ENABLED
from helpers.permissions import has_bot_manager_role


class TestCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        logger.info("Test commands cog loaded")

    @discord.slash_command(
        name='delete_draft_channels',
        description='[TEST] Delete all draft-chat*, red-team*, blue-team* channels'
    )
    @has_bot_manager_role()
    async def delete_draft_channels(
        self,
        ctx,
        dry_run: discord.Option(bool, "Preview what would be deleted without actually deleting", default=True)
    ):
        """Delete all draft-related channels (draft-chat*, red-team*, blue-team*) across the server."""
        await ctx.defer(ephemeral=True)

        draft_prefixes = ("draft-chat", "red-team", "blue-team")

        # Collect all matching channels across the entire server
        channels_to_delete = []
        for channel in ctx.guild.channels:
            if channel.name.lower().startswith(draft_prefixes):
                channels_to_delete.append(channel)

        if not channels_to_delete:
            await ctx.followup.send(
                "No draft channels found matching draft-chat*, red-team*, or blue-team*.",
                ephemeral=True
            )
            return

        channel_list = "\n".join(f"• #{c.name} ({c.category.name if c.category else 'no category'})" for c in channels_to_delete)

        if dry_run:
            await ctx.followup.send(
                f"**Dry run — would delete {len(channels_to_delete)} channel(s):**\n{channel_list}",
                ephemeral=True
            )
            return

        deleted = []
        errors = []
        for channel in channels_to_delete:
            try:
                await channel.delete(reason=f"Manual draft channel cleanup by {ctx.author.display_name}")
                deleted.append(channel.name)
                await asyncio.sleep(0.5)
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                errors.append(f"#{channel.name}: {e}")
                logger.warning(f"Failed to delete draft channel {channel.name}: {e}")

        summary = f"Deleted {len(deleted)} draft channel(s)."
        if errors:
            summary += f"\n\n{len(errors)} error(s):\n" + "\n".join(f"• {e}" for e in errors)
        logger.info(f"{ctx.author.display_name} deleted {len(deleted)} draft channels in guild {ctx.guild.id}")
        await ctx.followup.send(summary, ephemeral=True)


def setup(bot):
    if not TEST_MODE_ENABLED:
        logger.info("Test commands cog skipped (TEST_MODE_ENABLED is False)")
        return
    bot.add_cog(TestCommands(bot))
