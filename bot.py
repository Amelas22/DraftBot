import discord
import os
from discord.ext import commands
from dotenv import load_dotenv
from session import init_db, re_register_views
from modals import CubeSelectionModal
from utils import cleanup_sessions_task

is_cleanup_task_running = False

async def main():
    load_dotenv()

    # Required Intents
    intents = discord.Intents.default()
    intents.messages = True
    intents.message_content = True
    intents.guilds = True
    intents.members = True

    TOKEN = os.getenv("BOT_TOKEN")

    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        global is_cleanup_task_running
        if not is_cleanup_task_running:
            bot.loop.create_task(cleanup_sessions_task(bot))
            is_cleanup_task_running = True
            print("Cleanup task started.")

        print(f'Logged in as {bot.user}!')
        # Call re_register_views here and pass the bot instance
        #await re_register_views(bot)

    @bot.slash_command(name='startdraft', description='Start a team draft with random teams', guild_id=None)
    async def start_draft(interaction: discord.Interaction):
        await interaction.response.send_modal(CubeSelectionModal(session_type="random", title="Select Cube"))

    @bot.slash_command(name='premadedraft', description='Start a team draft with premade teams', guild_id=None)
    async def premade_draft(interaction: discord.Interaction):
        await interaction.response.send_modal(CubeSelectionModal(session_type="premade", title="Select Cube"))

    # Initialize the database before starting the bot
    await init_db()

    # Run the bot
    await bot.start(TOKEN)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

