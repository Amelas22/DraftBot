import discord
import os
import dotenv
from discord.ext import commands
from discord import app_commands

dotenv.load_dotenv()

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents, debug_guilds=[1097030241874096139])

draft_message_id = None  # Variable to store the draft message ID
sign_ups = []  # List to store the names of users who signed up

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}!')

# Global variables to store the draft message and channel IDs
draft_message_id = None
draft_channel_id = None

@bot.tree.command(name='startdraft', description='Start a Magic: The Gathering draft table')
async def start_draft(interaction: discord.Interaction):
    global draft_message_id, draft_channel_id
    await interaction.response.defer()
    draft_message = await interaction.followup.send('React to join the draft table!\n\n**Sign-Ups:**', ephemeral=False)
    draft_message_id = draft_message.id
    draft_channel_id = draft_message.channel.id  # Store the channel ID


async def update_draft_message():
    global sign_ups, draft_message_id, draft_channel_id
    if draft_message_id and draft_channel_id:  # Check if both IDs are set
        channel = bot.get_channel(draft_channel_id)  # Use the channel ID to fetch the channel
        if channel:
            try:
                message = await channel.fetch_message(draft_message_id)
                new_content = f'React to join the draft table!\n\n**Sign-Ups:**\n' + '\n'.join(sign_ups)
                await message.edit(content=new_content)
            except discord.NotFound:
                print(f"Message with ID {draft_message_id} not found.")
            except discord.Forbidden:
                print("Bot doesn't have permissions to edit the message or fetch the channel.")


@bot.event
async def on_raw_reaction_add(payload):
    global sign_ups, draft_message_id
    # Ensure the reaction is to the draft message
    if payload.message_id == draft_message_id:
        # No check for a specific emoji, any reaction will do
        channel = bot.get_channel(payload.channel_id)
        if channel:
            try:
                user = await bot.fetch_user(payload.user_id)
                display_name = user.display_name
                if display_name not in sign_ups:
                    sign_ups.append(display_name)
                    await update_draft_message()
            except Exception as e:
                print(f"An error occurred: {e}")


@bot.event
async def on_raw_reaction_remove(payload):
    global sign_ups, draft_message_id, draft_channel_id
    # Ensure the reaction removed is from the draft message
    if payload.message_id == draft_message_id:
        channel = bot.get_channel(draft_channel_id)
        if channel:
            try:
                user = await bot.fetch_user(payload.user_id)
                display_name = user.display_name
                # Remove the user from the sign-ups list if they are in it
                if display_name in sign_ups:
                    sign_ups.remove(display_name)
                    await update_draft_message()
            except Exception as e:
                print(f"An error occurred while removing a reaction: {e}")



# Replace 'YOUR BOT TOKEN HERE' with your actual bot token
bot.run(os.getenv("BOT_TOKEN"))
