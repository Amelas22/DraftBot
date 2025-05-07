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
from bot_registry import register_bot
from preference_service import PlayerPreferences

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

    register_bot(bot)
    
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
            setup_managers = await reconnect_draft_setup_sessions(bot)
            if setup_managers:
                logger.info(f"Created {len(setup_managers)} draft setup reconnection managers")
                bot.loop.create_task(monitor_reconnection_tasks(setup_managers, "setup"))
            else:
                logger.info("No draft setup sessions to reconnect")
                      
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
        bot.loop.create_task(delayed_log_collection(bot))

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
    async def delayed_log_collection(bot):
        # Wait 65 seconds
        await asyncio.sleep(65)
        try:
            # Reconnect to sessions needing log collection
            logger.info("Starting draft log collection reconnection...")
            log_tasks = await reconnect_recent_draft_sessions(bot)
            if log_tasks:
                logger.info(f"Created {len(log_tasks)} draft log collection reconnection tasks")
                await monitor_reconnection_tasks(log_tasks, "log collection")
            else:
                logger.info("No draft log collection sessions to reconnect")
        except Exception as e:
            logger.error(f"Error setting up draft reconnections: {e}")
            
    async def monitor_reconnection_tasks(managers, task_type=""):
        """Process managers sequentially with a 1-second delay between each"""
        try:
            logger.info(f"Starting to run {len(managers)} draft {task_type} reconnection tasks sequentially")
            for i, manager in enumerate(managers):
                try:
                    # Start this specific connection and create a task to monitor it
                    task = asyncio.create_task(manager.keep_connection_alive())
                    logger.info(f"Started task {i+1}/{len(managers)} for draft ID: {manager.draft_id}")
                    
                    # Add a 1-second delay before the next task
                    if i < len(managers) - 1:  # Don't delay after the last manager
                        await asyncio.sleep(3)
                        
                except Exception as e:
                    logger.error(f"Error starting task {i+1}/{len(managers)}: {e}")
            
            logger.info(f"All draft {task_type} reconnection managers have been started sequentially")
        except Exception as e:
            logger.error(f"Error during draft {task_type} reconnection sequence: {e}")

    await league_commands(bot)
    await scheduled_posts(bot)
    await load_extensions(bot)
    await init_db()
    await ensure_guild_id_in_tables()
    await setup_sticky_handler(bot)
    logger.info("Database initialized")
    # Create a delayed task for leaderboard refresh
    async def delayed_refresh():
        # Wait for the bot to fully connect to all guilds
        await bot.wait_until_ready()
        # Add an extra safety delay
        await asyncio.sleep(120)
        # Then refresh all leaderboards
        try:
            from cogs.leaderboard import refresh_all_leaderboards
            await refresh_all_leaderboards(bot)
            logger.info("Completed leaderboard refresh after startup")
        except Exception as e:
            logger.error(f"Error refreshing leaderboards on startup: {e}")
    
    # Add the task to the bot's event loop
    bot.loop.create_task(delayed_refresh())

    
    # Run the bot
    await bot.start(TOKEN)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

