from loguru import logger
import discord
import os
from discord.ext import commands
from dotenv import load_dotenv
from database.message_management import setup_sticky_handler
from session import init_db
from modals import CubeSelectionModal
from utils import cleanup_sessions_task
from commands import league_commands, scheduled_posts, swiss_draft_commands



# Configure loguru to handle all logs, and set up optional file logging
logger.add("discord_bot.log", rotation="500 MB")

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
        # Call re_register_views here and pass the bot instance
        from utils import re_register_views, re_register_challenges
        await re_register_views(bot)
        await re_register_challenges(bot)
        from teamfinder import re_register_teamfinder
        await re_register_teamfinder(bot)
        logger.info("Re-registered team finder")

    @bot.slash_command(name='startdraft', description='Start a team draft with random teams', guild_id=None)
    async def start_draft(interaction: discord.Interaction):
        logger.info("Received startdraft command")
        await interaction.response.send_modal(CubeSelectionModal(session_type="random"))

    @bot.slash_command(name='premadedraft', description='Start a team draft with premade teams', guild_id=None)
    async def premade_draft(interaction: discord.Interaction):
        logger.info("Received premadedraft command")
        await interaction.response.send_modal(CubeSelectionModal(session_type="premade"))

    
    await league_commands(bot)
    await scheduled_posts(bot)
    await swiss_draft_commands(bot)
    await init_db()
    logger.info("Database initialized")

    await setup_sticky_handler(bot)

    # Run the bot
    await bot.start(TOKEN)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

