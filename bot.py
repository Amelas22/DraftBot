import discord
import os
import dotenv
from datetime import datetime
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

    # Store the current time as the start time using datetime
    draft_start_time = datetime.now().timestamp()

    # Create the embed object
    embed = discord.Embed(
        title=f"Vintage Cube Team Draft Queue - Started <t:{int(draft_start_time)}:R>",
        description="React to join the draft table!",
        color=discord.Color.dark_magenta()  # You can choose a color that fits your server's theme
    )
    
    # Optionally add fields, images, etc. to the embed
    embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)
    embed.set_thumbnail(url=os.getenv("IMG_URL"))  # URL to a relevant image
    embed.set_footer(text="React below to sign up!")

    # Send the message as an embed
    draft_message = await interaction.followup.send(embed=embed, ephemeral=False)
    
    # Store the IDs for later reference
    draft_message_id = draft_message.id
    draft_channel_id = draft_message.channel.id


async def update_draft_message():
    global sign_ups, draft_message_id, draft_channel_id
    if draft_message_id and draft_channel_id:
        channel = bot.get_channel(draft_channel_id)
        if channel:
            try:
                message = await channel.fetch_message(draft_message_id)
                embed = message.embeds[0]  # Assuming there's at least one embed in the message
                if sign_ups:
                    sign_ups_str = '\n'.join(sign_ups)
                else:
                    sign_ups_str = 'No players yet.'
                embed.set_field_at(0, name="Sign-Ups", value=sign_ups_str, inline=False)
                await message.edit(embed=embed)
            except discord.NotFound:
                print(f"Message with ID {draft_message_id} not found.")
            except discord.Forbidden:
                print("Bot doesn't have permissions to edit the message or fetch the channel.")
            except Exception as e:
                print(f"An error occurred: {e}")


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
