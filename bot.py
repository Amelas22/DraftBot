import discord
import os
import dotenv
from datetime import datetime
from discord.ext import commands
from discord import app_commands

# Load the environment variables
dotenv.load_dotenv()

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.reactions = True

# Replace the token with your bot's token
TOKEN = os.getenv("BOT_TOKEN")
# Replace with your actual guild ID
GUILD_ID = 1097030241874096139

bot = commands.Bot(command_prefix="!", intents=intents, debug_guilds=[GUILD_ID])

# Variable to store the draft message ID and channel ID
draft_message_id = None
draft_channel_id = None

# Dictionary to store the sign-ups with user IDs as keys and display names as values
sign_ups = {}

# Constants for the button custom ID
SIGN_UP_BUTTON_ID = 'sign_up'

class SignUpButton(discord.ui.Button):
    def __init__(self, label, style, custom_id):
        super().__init__(style=style, label=label, custom_id=custom_id)
    
    async def callback(self, interaction: discord.Interaction):
        global sign_ups

        # Acknowledge the interaction immediately to avoid "This interaction failed" message
        await interaction.response.defer()

        user_id = interaction.user.id
        user_display_name = interaction.user.display_name

        # Toggle the sign-up state
        if user_id in sign_ups:
            # User is canceling their sign-up
            del sign_ups[user_id]
            self.style = discord.ButtonStyle.green
            self.label = 'Sign Up'
        else:
            # User is signing up
            sign_ups[user_id] = user_display_name
            self.style = discord.ButtonStyle.red
            self.label = 'Cancel Sign Up'
        
        # Update the embed and the button
        await update_draft_message(interaction.message, user_id)


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}!')

@bot.tree.command(name='startdraft', description='Start a Magic: The Gathering draft table')
async def start_draft(interaction: discord.Interaction):
    global draft_message_id, draft_channel_id
    await interaction.response.defer()

    # Create the embed object with the initial title
    draft_start_time = datetime.now().timestamp()
    embed = discord.Embed(
        title=f"Vintage Cube Team Draft Queue - Started <t:{int(draft_start_time)}:R>",
        description="Click the button to join the draft table!",
        color=discord.Color.dark_magenta()
    )
    embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)

    # Create a view with a sign-up button
    view = discord.ui.View()
    sign_up_button = SignUpButton(label='Sign Up', style=discord.ButtonStyle.green, custom_id=SIGN_UP_BUTTON_ID)
    view.add_item(sign_up_button)

    # Send the embed with the button
    draft_message = await interaction.followup.send(embed=embed, view=view)
    
    # Store the message and channel IDs
    draft_message_id = draft_message.id
    draft_channel_id = draft_message.channel.id

async def update_draft_message(message, user_id):
    global sign_ups
    embed = message.embeds[0]
    sign_ups_str = '\n'.join(sign_ups.values()) if sign_ups else 'No players yet.'
    embed.set_field_at(0, name="Sign-Ups", value=sign_ups_str, inline=False)
    
    # Update the button state based on the user's sign-up status
    button_label = 'Cancel Sign Up' if user_id in sign_ups else 'Sign Up'
    button_style = discord.ButtonStyle.red if user_id in sign_ups else discord.ButtonStyle.green
    view = discord.ui.View()
    view.add_item(SignUpButton(label=button_label, style=button_style, custom_id=SIGN_UP_BUTTON_ID))
    
    # Edit the message with the updated embed and view
    # Use 'followup' since 'defer' was called earlier
    await message.edit(embed=embed, view=view)


# Run the bot with the specified token
bot.run(TOKEN)
