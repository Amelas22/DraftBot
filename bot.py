from loguru import logger
import discord
import os
import sys
from discord.ext import commands
from dotenv import load_dotenv
from database.message_management import setup_sticky_handler
from database.db_session import init_db, ensure_guild_id_in_tables
from utils import cleanup_sessions_task, check_inactive_players_task
from commands import league_commands, scheduled_posts
from reconnect_drafts import reconnect_recent_draft_sessions, reconnect_draft_setup_sessions

# Configure loguru for all modules
logger.remove()  # Remove default handler
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)
logger.add(
    "logs/draftbot_{time}.log",
    rotation="500 MB",
    retention="1 week",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    enqueue=True,  # Makes it thread-safe
    backtrace=True,  # Detailed error traces
    diagnose=True   # Even more detailed error information
)

async def load_extensions(bot):
    for filename in os.listdir("./cogs"):
        if filename.endswith(".py") and not filename.startswith("_"):
            try:
                bot.load_extension(f"cogs.{filename[:-3]}")
                logger.info(f"Loaded extension: {filename[:-3]}")
            except Exception as e:
                logger.error(f"Failed to load extension {filename}: {e}")

async def main():
    load_dotenv()

    # Required Intents
    intents = discord.Intents.default()
    intents.messages = True
    intents.message_content = True
    intents.guilds = True
    intents.members = True
    intents.reactions = True

    TOKEN = os.getenv("BOT_TOKEN")

    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        try:
            await bot.sync_commands()
            logger.info("Successfully synced commands")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")
        
        bot.loop.create_task(cleanup_sessions_task(bot))
        bot.loop.create_task(check_inactive_players_task(bot))
        try:
            # Reconnect to sessions needing setup
            logger.info("Starting draft setup reconnection...")
            setup_tasks = await reconnect_draft_setup_sessions(bot)
            if setup_tasks:
                logger.info(f"Created {len(setup_tasks)} draft setup reconnection tasks")
                bot.loop.create_task(monitor_reconnection_tasks(setup_tasks, "setup"))
            else:
                logger.info("No draft setup sessions to reconnect")
                
            # Reconnect to sessions needing log collection
            logger.info("Starting draft log collection reconnection...")
            log_tasks = await reconnect_recent_draft_sessions(bot)
            if log_tasks:
                logger.info(f"Created {len(log_tasks)} draft log collection reconnection tasks")
                bot.loop.create_task(monitor_reconnection_tasks(log_tasks, "log collection"))
            else:
                logger.info("No draft log collection sessions to reconnect")
                
        except Exception as e:
            logger.error(f"Error setting up draft reconnections: {e}")

        from config import migrate_configs
        migrate_configs()
        print(f'Logged in as {bot.user}!')
        from utils import re_register_views
        await re_register_views(bot)
        from livedrafts import re_register_live_drafts
        await re_register_live_drafts(bot)
        logger.info("Re-registered team finder")

    @bot.event
    async def on_guild_join(guild):
        # Initialize config for the new guild
        from config import get_config
        config = get_config(guild.id)
        # Log the join
        print(f"Joined new guild: {guild.name} (ID: {guild.id})")
        
        # Try to send a welcome message to the system channel if available
        if guild.system_channel:
            await guild.system_channel.send(
                "Thanks for adding the Draft Bot! To set up needed channels and roles, an admin should use `/setup`."
            )
    async def monitor_reconnection_tasks(tasks, task_type=""):
        """Monitor the reconnection tasks without blocking bot startup"""
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info(f"All draft {task_type} reconnection tasks have completed")
        except Exception as e:
            logger.error(f"Error during draft {task_type} reconnection: {e}")

    await league_commands(bot)
    await scheduled_posts(bot)
    await load_extensions(bot)
    await init_db()
    await ensure_guild_id_in_tables()
    logger.info("Database initialized")

    await setup_sticky_handler(bot)
    # Run the bot
    await bot.start(TOKEN)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

