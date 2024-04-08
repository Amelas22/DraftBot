import discord
import os
from discord.ext import commands
from dotenv import load_dotenv
from session import init_db
from modals import CubeSelectionModal
from utils import cleanup_sessions_task
from commands import league_commands



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
        bot.loop.create_task(cleanup_sessions_task(bot))
        print(f'Logged in as {bot.user}!')
        # Call re_register_views here and pass the bot instance
        from utils import re_register_views, re_register_challenges
        await re_register_views(bot)
        await re_register_challenges(bot)

    @bot.slash_command(name='startdraft', description='Start a team draft with random teams', guild_id=None)
    async def start_draft(interaction: discord.Interaction):
        await interaction.response.send_modal(CubeSelectionModal(session_type="random", title="Select Cube"))

    @bot.slash_command(name='premadedraft', description='Start a team draft with premade teams', guild_id=None)
    async def premade_draft(interaction: discord.Interaction):
        cube_overseer_role = discord.utils.get(interaction.guild.roles, name="Cube Overseer")
    
        if cube_overseer_role in interaction.user.roles:
            await interaction.response.send_modal(CubeSelectionModal(session_type="premade", title="Select Cube"))
        else:
            # Responding with a message indicating lack of permission
            await interaction.response.send_message("Use `/leaguedraft` if you're trying to set up a league draft. This will ensure your results are properly tracked. If you need an untracked draft, tag Cube Overseer and they will set up the lobby.", ephemeral=True)
        
    
    await league_commands(bot)
    await init_db()

    # Run the bot
    await bot.start(TOKEN)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

