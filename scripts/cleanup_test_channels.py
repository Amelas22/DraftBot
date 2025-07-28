#!/usr/bin/env python3
"""
Test Channel Cleanup Script

This script removes draft-related channels from your test Discord server
to prevent channel bloat during testing. It identifies and deletes channels
created by the draft bot based on naming patterns and categories.

Usage:
    pipenv run python scripts/cleanup_test_channels.py

Safety Features:
- Only works in test mode (TEST_MODE_ENABLED = True in config.py)
- Prompts for confirmation before deleting channels
- Shows preview of channels to be deleted
- Logs all deletions for audit trail
"""

import asyncio
import discord
import sys
import os
from pathlib import Path
from loguru import logger
from dotenv import load_dotenv

# Add parent directory to path to import project modules
sys.path.append(str(Path(__file__).parent.parent))

from config import TEST_MODE_ENABLED, get_config

# Load environment variables
load_dotenv()

# Channel naming patterns to identify draft-related channels
DRAFT_CHANNEL_PATTERNS = [
    "draft-",
    "team-",
    "match-",
    "round-",
    "game-",
    "voice-draft-"
]

# Category names that contain draft channels
DRAFT_CATEGORIES = [
    "Draft Channels",
    "Team Drafts",
    "Active Drafts",
    "Draft Matches"
]

class ChannelCleanup:
    def __init__(self, bot):
        self.bot = bot
        self.channels_to_delete = []
        self.categories_to_delete = []
        
    async def find_draft_channels(self, guild):
        """Find all draft-related channels and categories in the guild"""
        channels = []
        categories = []
        
        # Find channels matching draft patterns
        for channel in guild.channels:
            if isinstance(channel, discord.TextChannel) or isinstance(channel, discord.VoiceChannel):
                channel_name = channel.name.lower()
                
                # Check if channel name matches any draft pattern
                if any(pattern in channel_name for pattern in DRAFT_CHANNEL_PATTERNS):
                    channels.append(channel)
                    
                # Check if channel is in a draft category
                elif channel.category and channel.category.name in DRAFT_CATEGORIES:
                    channels.append(channel)
                    
            elif isinstance(channel, discord.CategoryChannel):
                # Find draft-related categories
                if channel.name in DRAFT_CATEGORIES:
                    categories.append(channel)
                    
        return channels, categories
    
    def preview_deletion(self, guild, channels, categories):
        """Show a preview of what will be deleted"""
        if not channels and not categories:
            logger.info(f"No draft channels found in {guild.name}")
            return False
            
        logger.info(f"\n=== CLEANUP PREVIEW for {guild.name} ===")
        
        if channels:
            logger.info(f"\nChannels to delete ({len(channels)}):")
            for channel in channels:
                channel_type = "Text" if isinstance(channel, discord.TextChannel) else "Voice"
                category_name = channel.category.name if channel.category else "No Category"
                logger.info(f"  - {channel_type}: #{channel.name} (in {category_name})")
                
        if categories:
            logger.info(f"\nCategories to delete ({len(categories)}):")
            for category in categories:
                channel_count = len(category.channels)
                logger.info(f"  - Category: {category.name} ({channel_count} channels)")
                
        return True
    
    async def cleanup_channels(self, guild, channels, categories, dry_run=False):
        """Delete the identified channels and categories"""
        if dry_run:
            logger.info("DRY RUN - No channels will actually be deleted")
            return
            
        deleted_count = 0
        failed_count = 0
        
        # Delete individual channels first
        for channel in channels:
            try:
                logger.info(f"Deleting channel: #{channel.name}")
                await channel.delete(reason="Test environment cleanup")
                deleted_count += 1
                # Small delay to avoid rate limits
                await asyncio.sleep(0.5)
            except discord.Forbidden:
                logger.error(f"No permission to delete channel: #{channel.name}")
                failed_count += 1
            except discord.HTTPException as e:
                logger.error(f"Failed to delete channel #{channel.name}: {e}")
                failed_count += 1
                
        # Delete empty categories
        for category in categories:
            try:
                # Only delete if category is empty
                if len(category.channels) == 0:
                    logger.info(f"Deleting empty category: {category.name}")
                    await category.delete(reason="Test environment cleanup")
                    deleted_count += 1
                else:
                    logger.warning(f"Skipping non-empty category: {category.name}")
            except discord.Forbidden:
                logger.error(f"No permission to delete category: {category.name}")
                failed_count += 1
            except discord.HTTPException as e:
                logger.error(f"Failed to delete category {category.name}: {e}")
                failed_count += 1
                
        logger.info(f"\nCleanup complete: {deleted_count} deleted, {failed_count} failed")
        
    async def run_cleanup(self, guild_id=None, dry_run=False, auto_confirm=False):
        """Main cleanup function"""
        
        # Safety check - only run in test mode
        if not TEST_MODE_ENABLED:
            logger.error("ERROR: This script only runs when TEST_MODE_ENABLED = True in config.py")
            logger.error("This prevents accidental deletion of production channels.")
            return False
            
        logger.info("Starting test channel cleanup...")
        logger.info("TEST_MODE_ENABLED = True - Proceeding with cleanup")
        
        # Get guilds to clean up
        guilds_to_process = []
        if guild_id:
            guild = self.bot.get_guild(int(guild_id))
            if guild:
                guilds_to_process.append(guild)
            else:
                logger.error(f"Guild with ID {guild_id} not found")
                return False
        else:
            guilds_to_process = self.bot.guilds
            
        if not guilds_to_process:
            logger.error("No guilds found to process")
            return False
            
        # Process each guild
        total_channels = 0
        total_categories = 0
        
        for guild in guilds_to_process:
            logger.info(f"\nProcessing guild: {guild.name} (ID: {guild.id})")
            
            # Find draft channels
            channels, categories = await self.find_draft_channels(guild)
            
            if not channels and not categories:
                logger.info(f"No draft channels found in {guild.name}")
                continue
                
            # Show preview
            has_items = self.preview_deletion(guild, channels, categories)
            if not has_items:
                continue
                
            total_channels += len(channels)
            total_categories += len(categories)
            
            # Get confirmation unless auto-confirmed or dry run
            if not auto_confirm and not dry_run:
                response = input(f"\nProceed with deletion in {guild.name}? (y/N): ").strip().lower()
                if response not in ['y', 'yes']:
                    logger.info(f"Skipping {guild.name}")
                    continue
                    
            # Perform cleanup
            await self.cleanup_channels(guild, channels, categories, dry_run)
            
        if dry_run:
            logger.info(f"\nDRY RUN SUMMARY: Would delete {total_channels} channels and {total_categories} categories")
        else:
            logger.info(f"\nCLEANUP SUMMARY: Processed {total_channels} channels and {total_categories} categories")
            
        return True

async def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Clean up test draft channels")
    parser.add_argument("--guild-id", help="Specific guild ID to clean up (optional)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting")
    parser.add_argument("--yes", action="store_true", help="Auto-confirm deletion (dangerous!)")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    
    args = parser.parse_args()
    
    # Configure logging
    if args.verbose:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG")
    
    # Safety check
    if not TEST_MODE_ENABLED:
        logger.error("\n" + "="*60)
        logger.error("SAFETY CHECK FAILED")
        logger.error("TEST_MODE_ENABLED must be True in config.py")
        logger.error("This prevents accidental deletion of production channels")
        logger.error("="*60)
        return 1
    
    # Warn about auto-confirm
    if args.yes and not args.dry_run:
        logger.warning("AUTO-CONFIRM enabled - channels will be deleted without prompting!")
        await asyncio.sleep(2)  # Give user time to read warning
    
    # Get Discord token
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("BOT_TOKEN not found in environment variables")
        return 1
    
    # Create Discord client
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    client = discord.Client(intents=intents)
    
    @client.event
    async def on_ready():
        logger.info(f"Bot connected as {client.user}")
        
        try:
            # Create cleanup instance and run
            cleanup = ChannelCleanup(client)
            success = await cleanup.run_cleanup(
                guild_id=args.guild_id,
                dry_run=args.dry_run,
                auto_confirm=args.yes
            )
            
            # Store result and close
            client._cleanup_success = success
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
            client._cleanup_success = False
        finally:
            await client.close()
    
    try:
        # Start the client with timeout
        await asyncio.wait_for(client.start(token), timeout=30.0)
        return 0 if getattr(client, '_cleanup_success', False) else 1
        
    except asyncio.TimeoutError:
        logger.error("Connection timeout - make sure your bot token is valid and you have internet connection")
        return 1
    except KeyboardInterrupt:
        logger.info("Cleanup interrupted by user")
        return 1
    except Exception as e:
        logger.error(f"Unexpected error during cleanup: {e}")
        return 1
    finally:
        if not client.is_closed():
            await client.close()

if __name__ == "__main__":
    # Ensure we're using pipenv
    if "VIRTUAL_ENV" not in os.environ and "PIPENV_ACTIVE" not in os.environ:
        logger.error("Please run this script with 'pipenv run python scripts/cleanup_test_channels.py'")
        sys.exit(1)
        
    exit_code = asyncio.run(main())
    sys.exit(exit_code)