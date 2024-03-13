import discord
import os
import dotenv
from datetime import datetime
from discord.ext import commands
import random
import secrets

# Load the environment variables
dotenv.load_dotenv()

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True

TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))

bot = commands.Bot(command_prefix="!", intents=intents, debug_guilds=[GUILD_ID])

draft_message_id = None
draft_channel_id = None
draft_link = None
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

class DraftCompleteButton(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.green, label="Draft Complete", custom_id="draft_complete")

    async def callback(self, interaction: discord.Interaction):
        global sign_ups

        user_id = interaction.user.id
        # Check if the user is in the sign-up list
        if user_id not in sign_ups:
            await interaction.response.send_message("You are not authorized to complete the draft.", ephemeral=True)
            return
        
        guild = interaction.guild

        team_a, team_b = split_into_teams(list(sign_ups.values()))
        team_a_members = [guild.get_member(user_id) for user_id in sign_ups if sign_ups[user_id] in team_a]
        team_b_members = [guild.get_member(user_id) for user_id in sign_ups if sign_ups[user_id] in team_b]

        team_a_channel = await create_team_channel(guild, "Team A Chat", team_a_members)
        team_b_channel = await create_team_channel(guild, "Team B Chat", team_b_members)

        await interaction.response.send_message(f"Team channels created: {team_a_channel.mention} and {team_b_channel.mention}", ephemeral=True)


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
        # Initializes the button with the label "Start Draft"
        super().__init__(style=discord.ButtonStyle.blurple, label="Start Draft", custom_id='start_draft')

    async def callback(self, interaction: discord.Interaction):
        global sign_ups, draft_link

        # Check if there are participants
        if not sign_ups:
            await interaction.response.send_message("There are no participants to start the draft.", ephemeral=True)
            return

        team_a, team_b = split_into_teams(list(sign_ups.values()))
        seating_order = generate_seating_order(team_a, team_b)

        # Create the embed message for the draft
        embed = discord.Embed(
            title="Draft is Ready!",
            description=f"**Team A**:\n" + "\n".join(team_a) + 
                         "\n\n**Team B**:\n" + "\n".join(team_b) + 
                         "\n\n**Seating Order:**\n" + " -> ".join(seating_order) +
                         f"\n\n**Draftmancer Session**: **[Join Here]({draft_link})**",
            color=discord.Color.gold()
        )

        # Remove the "Start Draft" button and add the "Draft Complete" button
        view = discord.ui.View()
        view.add_item(DraftCompleteButton())  # Assumes DraftCompleteButton is defined
        await interaction.response.edit_message(embed=embed, view=view)


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}!')

import secrets

@bot.tree.command(name='startdraft', description='Start a Magic: The Gathering draft table')
async def start_draft(interaction: discord.Interaction):
    await interaction.response.defer()
    global draft_message_id, draft_channel_id, draft_link
    
    # Generate and store the Draftmancer link
    session_id = ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
    draft_link = f"https://draftmancer.com/?session=DB{session_id}"
    
    draft_start_time = datetime.now().timestamp()
    embed = discord.Embed(
        title=f"Vintage Cube Team Draft Queue - Started <t:{int(draft_start_time)}:R>",
        description=f"Click the button to join the draft table!\n\n**Draftmancer Session**: **[Join Here]({draft_link})**",
        color=discord.Color.dark_magenta()
    )
    embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)
    embed.set_thumbnail(url=os.getenv("IMG_URL"))

    view = discord.ui.View()
    view.add_item(SignUpButton())
    view.add_item(CancelSignUpButton())
    view.add_item(CancelDraftButton())
    view.add_item(GenerateDraftmancerLinkButton())

    message = await interaction.followup.send(embed=embed, view=view)
    draft_message_id = message.id
    draft_channel_id = message.channel.id

async def create_team_channel(guild, team_name, team_members):
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False)
    }
    overwrites.update({member: discord.PermissionOverwrite(read_messages=True) for member in team_members})

    channel = await guild.create_text_channel(name=team_name, overwrites=overwrites)
    return channel

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
