import discord
import os
from discord.ext import commands
from dotenv import load_dotenv
from commands import setup_commands
from sessions import cleanup_sessions_task, load_sessions_from_file, periodic_save_sessions, sessions


load_dotenv()

# Required Intents
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True

TOKEN = os.getenv("BOT_TOKEN")

is_cleanup_task_running = False
is_periodic_save_sessions_running = False

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    global is_cleanup_task_running, is_periodic_save_sessions_running
    if not is_cleanup_task_running:
        bot.loop.create_task(cleanup_sessions_task())
        is_cleanup_task_running = True
    
    if not is_periodic_save_sessions_running:
        bot.loop.create_task(periodic_save_sessions())
        is_periodic_save_sessions_running = True

    sessions = load_sessions_from_file()  # Load sessions from file at startup
    
    print(f'Logged in as {bot.user}!')
    setup_commands(bot)

if __name__ == "__main__":
    bot.run(TOKEN)