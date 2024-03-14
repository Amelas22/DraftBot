import discord
import asyncio
import os
import dotenv
from datetime import datetime, timedelta
from discord.ext import commands
from discord.ui import Button, View
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

session_id  = None
draft_message_id = None
draft_channel_id = None
draft_link = None
draft_start_time = None
draft_chat_channel = None
sign_ups = {}

class SignUpButton(Button):
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
            await interaction.response.send_message("You are now signed up.", ephemeral=True)

        await update_draft_message(interaction.message, interaction.user.id)


class CancelSignUpButton(Button):
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
            await interaction.response.send_message("Your sign-up has been canceled.", ephemeral=True)

        await update_draft_message(interaction.message, interaction.user.id)

class DraftCompleteButton(Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.green, label="Draft Complete", custom_id="draft_complete")

    async def callback(self, interaction: discord.Interaction):
        global sign_ups, session_id

        await interaction.response.defer(ephemeral=True)

        if not sign_ups:
            await interaction.followup.send("There are no participants to start the draft.", ephemeral=True)
            return

        guild = interaction.guild
        team_a_ids, team_b_ids = split_into_teams(list(sign_ups.keys()))
        team_a_members = [guild.get_member(user_id) for user_id in team_a_ids]
        team_b_members = [guild.get_member(user_id) for user_id in team_b_ids]
        all_members = [guild.get_member(user_id) for user_id in sign_ups.keys()]

        # Gather all tasks to be executed
        tasks = [
            create_team_channel(guild, "Team-A", team_a_members, session_id),
            create_team_channel(guild, "Team-B", team_b_members, session_id),
            create_team_channel(guild, "Draft-chat", all_members, session_id)
        ]

        # Wait for all tasks to complete
        team_a_channel, team_b_channel, draft_chat_channel = await asyncio.gather(*tasks)
        
        message = await interaction.channel.fetch_message(interaction.message.id)
        view = message.components  # Get the current view from the message
        
        # Find and enable the PostPairingsButton
        for item in view:
            if isinstance(item, PostPairingsButton):
                item.disabled = False  # Enable the button
 
        await message.edit(view=view)
        await interaction.response.send_message("Draft complete. You can now post pairings.", ephemeral=True)


class CancelDraftButton(Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.grey, label='Cancel Draft', custom_id='cancel_draft')

    async def callback(self, interaction: discord.Interaction):
        global draft_message_id, draft_channel_id, sign_ups  

        user_id = interaction.user.id

        if user_id in sign_ups or not sign_ups:
            await interaction.message.delete()
            draft_message_id = None
            draft_channel_id = None
            sign_ups = {}
        else:
            await interaction.response.send_message("You cannot cancel the draft as you are not signed up or others are signed up.", ephemeral=True)


class GenerateDraftmancerLinkButton(Button):
    def __init__(self):
        # Initializes the button with the label "Randomize Teams"
        super().__init__(style=discord.ButtonStyle.blurple, label="Randomize Teams", custom_id='start_draft')

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

        # Remove the "Start Draft" button and add the "Draft Complete" and "Post Pairings" button
        view = View()
        view.add_item(DraftCompleteButton())
        view.add_item(PostPairingsButton())

        await interaction.response.edit_message(embed=embed, view=view)


class PostPairingsButton(Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.primary, label="Post Pairings", custom_id="post_pairings")

    async def callback(self, interaction: discord.Interaction):
        global draft_chat_channel, sign_ups

        if draft_chat_channel is None:
            # Draft not completed yet, inform the user
            await interaction.response.send_message("Pairings can't be posted until the draft is completed.", ephemeral=True)
            return

        # Split sign-ups into team A and team B
        team_a_ids, team_b_ids = split_into_teams(list(sign_ups.keys()))

        # Generate pairings
        pairings = calculate_pairings(team_a_ids, team_b_ids)

        # Post the pairings
        guild = interaction.guild
        draft_chat_channel_obj = guild.get_channel(draft_chat_channel)
        if draft_chat_channel_obj:
            await post_pairings(draft_chat_channel_obj, pairings, guild)
            await interaction.response.send_message("Pairings have been posted in the draft chat.", ephemeral=True)
        else:
            await interaction.response.send_message("Draft chat channel not found.", ephemeral=True)


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}!')


@bot.tree.command(name='startdraft', description='Start a Magic: The Gathering draft table')
async def start_draft(interaction: discord.Interaction):
    await interaction.response.defer()
    global draft_message_id, draft_channel_id, draft_link, session_id, draft_start_time
    
    # Generate and store the Draftmancer link
    session_id = ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
    draft_link = f"https://draftmancer.com/?session=DB{session_id}"
    
    draft_start_time = datetime.now().timestamp()

    # Find the "Cube Drafter" role by name
    cube_drafter_role = discord.utils.get(interaction.guild.roles, name="Cube Drafter")
    
    # Prepare the ping message
    ping_message = f"{cube_drafter_role.mention if cube_drafter_role else 'Cube Drafter'} Vintage Cube Draft Queue Open!"
    
    # Send the ping message to role or "Cube Drafter" if role not found
    await interaction.followup.send(ping_message, ephemeral=False)

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


async def create_team_channel(guild, team_name, team_members, session_id=None):
    global draft_chat_channel

    # Find the chat category by name
    draft_category = discord.utils.get(guild.categories, name="Draft Channels")
    
    channel_name = f"{team_name}-Draft-{session_id}" if session_id else f"{team_name}-Draft"
    
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True)
    }
    for member in team_members:
        overwrites[member] = discord.PermissionOverwrite(read_messages=True)

    # Specify the category in the channel creation
    channel = await guild.create_text_channel(name=channel_name, overwrites=overwrites, category=draft_category)

    if team_name == "Draft-chat":
        draft_chat_channel = channel.id

    draft_start_datetime = datetime.fromtimestamp(draft_start_time)
    deletion_time = draft_start_datetime + timedelta(hours=6)
    
    # Use deletion_time to format message
    deletion_notice = await channel.send(f"This channel will be deleted <t:{int(deletion_time.timestamp())}:R>.")
    
    # Wait for 6 hours before deleting the channel and updating the deletion notice
    await asyncio.sleep(6 * 3600)
    await deletion_notice.edit(content="This channel is being deleted now.")
    await channel.delete()

    return channel

async def update_draft_message(message, user_id=None):
    embed = message.embeds[0]
    sign_ups_count = len(sign_ups)
    sign_ups_field_name = f"Sign-Ups ({sign_ups_count}):" if sign_ups else "Sign-Ups (0):"
    sign_ups_str = '\n'.join(sign_ups.values()) if sign_ups else 'No players yet.'
    embed.set_field_at(0, name=sign_ups_field_name, value=sign_ups_str, inline=False)

    view = View()
    view.add_item(SignUpButton())
    view.add_item(CancelSignUpButton())
    view.add_item(CancelDraftButton())
    view.add_item(GenerateDraftmancerLinkButton())

    await message.edit(embed=embed, view=view)

async def post_pairings(channel, pairings, guild):
    # Ensure member mentions are enabled in the channel
    await channel.edit(slowmode_delay=0) 
    
    for round_number, round_pairings in pairings.items():
        # Create an embed for the round
        embed = discord.Embed(title=f"Round {round_number} Pairings", color=discord.Color.blue())
        
        for player_id, opponent_id in round_pairings:
            player = guild.get_member(player_id)
            opponent = guild.get_member(opponent_id)
            player_name = player.display_name if player else 'Unknown'
            opponent_name = opponent.display_name if opponent else 'Unknown'
            # Add each pairing as a field in the embed
            embed.add_field(name=f"Match {round_pairings.index((player_id, opponent_id)) + 1}", value=f"{player_name} vs {opponent_name}", inline=False)
        
        # Send the embed for the current round
        await channel.send(embed=embed)

    # Construct a message tagging all participants
    sign_up_tags = ' '.join([guild.get_member(user_id).mention for user_id in sign_ups.keys() if guild.get_member(user_id)])
    await channel.send(f"{sign_up_tags}\nPairings Posted Above")


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

def calculate_pairings(team_a_ids, team_b_ids):
    """
    Calculate pairings for a three-round tournament where each member of team A faces a unique member of team B.
    team_a_ids and team_b_ids must be of the same length.
    """
    assert len(team_a_ids) == len(team_b_ids), "Teams must be of equal size"
    total_players = len(team_a_ids)
    pairings = {1: [], 2: [], 3: []}

    # Initial pairings for round 1
    for a, b in zip(team_a_ids, team_b_ids):
        pairings[1].append((a, b))

    # Generate pairings for subsequent rounds
    for round_number in [2, 3]:
        # Rotate Team B members to get new pairings
        team_b_ids = team_b_ids[1:] + team_b_ids[:1]
        for a, b in zip(team_a_ids, team_b_ids):
            pairings[round_number].append((a, b))

    return pairings


bot.run(TOKEN)
