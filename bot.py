from loguru import logger
import discord
import os
import sys
from discord.ext import commands
from dotenv import load_dotenv
from database.message_management import setup_sticky_handler
from session import init_db, ensure_guild_id_in_tables
from modals import CubeDraftSelectionView
from utils import cleanup_sessions_task
from commands import league_commands, scheduled_posts

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
        bot.loop.create_task(cleanup_sessions_task(bot))
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

    @bot.slash_command(name='startdraft', description='Start a team draft with random teams', guild_id=None)
    async def start_draft(interaction: discord.Interaction):
        logger.info("Received startdraft command")
        view = CubeDraftSelectionView(session_type="random")
        await interaction.response.send_message("Select a cube:", view=view, ephemeral=True)

    @bot.slash_command(name='premadedraft', description='Start a team draft with premade teams', guild_id=None)
    async def premade_draft(interaction: discord.Interaction):
        logger.info("Received premadedraft command")
        view = CubeDraftSelectionView(session_type="premade")
        await interaction.response.send_message("Select a cube:", view=view, ephemeral=True)
        
    @bot.slash_command(name='dynamic_stake', description='Start a team draft with random teams and customizable stakes')
    async def staked_draft(interaction: discord.Interaction):
        logger.info("Received stakedraft command")
        
        from modals import StakedCubeDraftSelectionView
        view = StakedCubeDraftSelectionView()
        await interaction.response.send_message("Select a cube for the staked draft:", view=view, ephemeral=True)
    
    await league_commands(bot)
    await scheduled_posts(bot)
    await init_db()
    await ensure_guild_id_in_tables()
    logger.info("Database initialized")

    await setup_sticky_handler(bot)

    # Run the bot
    await bot.start(TOKEN)



if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

