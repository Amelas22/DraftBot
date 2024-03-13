import discord
import os
import dotenv
from datetime import datetime
from discord.ext import commands
import random

# Load the environment variables
dotenv.load_dotenv()

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.reactions = True

TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))

bot = commands.Bot(command_prefix="!", intents=intents, debug_guilds=[GUILD_ID])

draft_message_id = None
draft_channel_id = None
sign_ups = {}

class SignUpButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.green, label="Sign Up", custom_id="sign_up")

    async def callback(self, interaction: discord.Interaction):
        global sign_ups

        user_id = interaction.user.id
        if user_id in sign_ups:
            # User is already signed up; inform them
            await interaction.response.send_message("You are already signed up!", ephemeral=True)
        else:
            # User is signing up
            sign_ups[user_id] = interaction.user.display_name
            # Optionally, send a confirmation message or update the sign-up list in the main message
            await interaction.response.send_message("You are now signed up.", ephemeral=True)

        await update_draft_message(interaction.message, interaction.user.id)


class CancelSignUpButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.red, label="Cancel Sign Up", custom_id="cancel_sign_up")

    async def callback(self, interaction: discord.Interaction):
        global sign_ups

        user_id = interaction.user.id
        if user_id not in sign_ups:
            # User is not signed up; inform them
            await interaction.response.send_message("You are not signed up!", ephemeral=True)
        else:
            # User is canceling their sign-up
            del sign_ups[user_id]
            # Optionally, send a confirmation message or update the sign-up list in the main message
            await interaction.response.send_message("Your sign-up has been canceled.", ephemeral=True)

        await update_draft_message(interaction.message, interaction.user.id)


class CancelDraftButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.grey, label='Cancel Draft', custom_id='cancel_draft')

    async def callback(self, interaction: discord.Interaction):
        global draft_message_id, draft_channel_id, sign_ups  # Correct use of global

        user_id = interaction.user.id

        if user_id in sign_ups or not sign_ups:
            await interaction.message.delete()
            draft_message_id = None
            draft_channel_id = None
            sign_ups = {}
        else:
            await interaction.response.send_message("You cannot cancel the draft as you are not signed up or others are signed up.", ephemeral=True)


class GenerateDraftmancerLinkButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.blurple, label='Generate Draftmancer Link', custom_id='generate_draftmancer_link')
    
    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id in sign_ups or not sign_ups:
            team_a, team_b = split_into_teams(list(sign_ups.values()))
            seating_order = generate_seating_order(team_a, team_b)
            draft_link = f"https://draftmancer.com/?session=divcube{random.randint(1, 999)}"
            
            embed = discord.Embed(
                title="Draft is Ready!",
                description=f"**Team A**:\n" + "\n".join(team_a) + "\n\n**Team B**:\n" + "\n".join(team_b) + "\n\n**Seating Order:**\n" + " -> ".join(seating_order) + f"\n\n**Draftmancer**: [Join Draft]({draft_link})",
                color=discord.Color.gold()
            )
            embed.set_thumbnail(url=os.getenv("IMG_URL"))
            await interaction.response.edit_message(embed=embed, view=None)
            await interaction.followup.send("DRAFT READY: " + ' '.join([f"<@{uid}>" for uid in sign_ups.keys()]), allowed_mentions=discord.AllowedMentions(users=True))
        else:
            await interaction.response.send_message("Only participants can generate the Draftmancer link.", ephemeral=True)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}!')

@bot.tree.command(name='startdraft', description='Start a Magic: The Gathering draft table')
async def start_draft(interaction: discord.Interaction):
    await interaction.response.defer()

    draft_start_time = datetime.now().timestamp()
    embed = discord.Embed(
        title=f"Vintage Cube Team Draft Queue - Started <t:{int(draft_start_time)}:R>",
        description="Click the button to join the draft table!",
        color=discord.Color.dark_magenta()
    )
    embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)
    embed.set_thumbnail(url=os.getenv("IMG_URL"))

    view = discord.ui.View()
    view.add_item(SignUpButton())
    view.add_item(CancelSignUpButton())
    view.add_item(CancelDraftButton())
    view.add_item(GenerateDraftmancerLinkButton())

    draft_message = await interaction.followup.send(embed=embed, view=view)
    global draft_message_id, draft_channel_id
    draft_message_id = draft_message.id
    draft_channel_id = draft_message.channel.id

async def update_draft_message(message, user_id=None):
    embed = message.embeds[0]
    sign_ups_count = len(sign_ups)
    sign_ups_field_name = f"Sign-Ups ({sign_ups_count}):" if sign_ups else "Sign-Ups (0):"
    sign_ups_str = '\n'.join(sign_ups.values()) if sign_ups else 'No players yet.'
    embed.set_field_at(0, name=sign_ups_field_name, value=sign_ups_str, inline=False)

    view = discord.ui.View()
    view.add_item(SignUpButton())
    view.add_item(CancelSignUpButton())
    view.add_item(CancelDraftButton())
    view.add_item(GenerateDraftmancerLinkButton())

    await message.edit(embed=embed, view=view)

def split_into_teams(signups):
    random.shuffle(signups)
    mid = len(signups) // 2
    return signups[:mid], signups[mid:]

def generate_seating_order(team_a, team_b):
    seating_order = []
    for i in range(max(len(team_a), len(team_b))):
        if i < len(team_a):
            seating_order.append(team_a[i])
        if i < len(team_b):
            seating_order.append(team_b[i])
    return seating_order

bot.run(TOKEN)
