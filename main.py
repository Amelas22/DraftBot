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

bot = commands.Bot(command_prefix="!", intents=intents)

sessions = {}

class DraftSession:
    def __init__(self, session_id):
        self.session_id = session_id
        self.message_id = None
        self.draft_channel_id = None
        self.draft_message_id = None
        self.draft_link = None
        self.draft_start_time = datetime.now()
        self.deletion_time = datetime.now() + timedelta(hours=7)
        self.draft_chat_channel = None
        self.guild_id = None
        self.draft_id = None
        self.team_a = None
        self.team_b = None
        self.sign_ups = {}
        self.channel_ids = []

    async def update_draft_message(self, interaction):
        message = await interaction.channel.fetch_message(self.message_id)
        embed = message.embeds[0]
        sign_ups_count = len(self.sign_ups)
        sign_ups_field_name = f"Sign-Ups ({sign_ups_count}):" if self.sign_ups else "Sign-Ups (0):"
        sign_ups_str = '\n'.join(self.sign_ups.values()) if self.sign_ups else 'No players yet.'
        embed.set_field_at(0, name=sign_ups_field_name, value=sign_ups_str, inline=False)

        await message.edit(embed=embed)


    async def create_team_channel(self, guild, team_name, team_members, team_a, team_b):
        draft_category = discord.utils.get(guild.categories, name="Draft Channels")
        channel_name = f"{team_name}-Chat-{self.draft_id}"

        # Retrieve the "Cube Overseer" role
        overseer_role = discord.utils.get(guild.roles, name="Cube Overseer")
        
        # Basic permissions overwrites for the channel
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True)
        }

        # If it's a team channel, adjust permissions specifically for overseers in the draft
        if team_name in ["Team-A", "Team-B"]:
            # Add all overseers with read permission initially
            if overseer_role:
                overwrites[overseer_role] = discord.PermissionOverwrite(read_messages=True)
            
            participating_overseers = [member for member in overseer_role.members if member.id in team_a or member.id in team_b]
            for overseer in participating_overseers:
                # Remove access for overseers who are part of the other team
                if (team_name == "Team-A" and overseer.id in team_b) or (team_name == "Team-B" and overseer.id in team_a):
                    overwrites[overseer] = discord.PermissionOverwrite(read_messages=False)
        
        # Add team members with read permission. This specifically allows these members, overriding role-based permissions if needed.
        for member in team_members:
            overwrites[member] = discord.PermissionOverwrite(read_messages=True)
        
        # Create the channel with the specified overwrites
        channel = await guild.create_text_channel(name=channel_name, overwrites=overwrites, category=draft_category)
        self.channel_ids.append(channel.id)

        if team_name == "Draft-chat":
            self.draft_chat_channel = channel.id

           
    async def update_draft_complete_message(self, interaction):
        await interaction.followup.send("Draft complete. You can now post pairings.", ephemeral=True)
        
    async def post_pairings(self, guild, pairings):
        if not self.draft_chat_channel:
            print("Draft chat channel not set.")
            return

        draft_chat_channel_obj = guild.get_channel(self.draft_chat_channel)
        if not draft_chat_channel_obj:
            print("Draft chat channel not found.")
            return

        # Ensure member mentions are enabled in the channel
        await draft_chat_channel_obj.edit(slowmode_delay=0)
        
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
            await draft_chat_channel_obj.send(embed=embed)

        # Construct a message tagging all participants
        sign_up_tags = ' '.join([guild.get_member(user_id).mention for user_id in self.sign_ups.keys() if guild.get_member(user_id)])
        await draft_chat_channel_obj.send(f"{sign_up_tags}\nPairings Posted Above")
    
    def calculate_pairings(self):
        # Ensure team lists are of equal size and convert to lists if they're not already
        team_a = list(self.team_a)
        team_b = list(self.team_b)
        assert len(team_a) == len(team_b), "Teams must be of equal size."

        pairings = {1: [], 2: [], 3: []}
        total_players = len(team_a)

        # Shuffle teams to randomize matchups
        random.shuffle(team_a)
        random.shuffle(team_b)

        # Schedule round-robin
        for round_number in range(1, 4):
            for i, player_a in enumerate(team_a):
                opponent_index = (i + round_number - 1) % total_players
                pairings[round_number].append((player_a, team_b[opponent_index]))

        return pairings

    
    def split_into_teams(self):
        sign_ups_list = list(self.sign_ups.keys())
        random.shuffle(sign_ups_list)
        mid_point = len(sign_ups_list) // 2
        self.team_a = sign_ups_list[:mid_point]
        self.team_b = sign_ups_list[mid_point:]
    
    async def generate_seating_order(self):
        guild = bot.get_guild(self.guild_id)
        team_a_members = [guild.get_member(user_id) for user_id in self.team_a]
        team_b_members = [guild.get_member(user_id) for user_id in self.team_b]

        seating_order = []
        for i in range(max(len(team_a_members), len(team_b_members))):
            if i < len(team_a_members) and team_a_members[i]:
                seating_order.append(team_a_members[i].display_name)
            if i < len(team_b_members) and team_b_members[i]:
                seating_order.append(team_b_members[i].display_name)
        return seating_order
    
    async def move_message_to_draft_channel(self, bot, original_channel_id, original_message_id, draft_chat_channel_id):
        original_channel = bot.get_channel(original_channel_id)
        if not original_channel:
            print(f"Original channel {original_channel_id} not found.")
            return
        try:
            original_message = await original_channel.fetch_message(original_message_id)
        except discord.NotFound:
            print(f"Message {original_message_id} not found in channel {original_channel_id}.")
            return

        # Check if the draft chat channel is set and exists
        draft_chat_channel = bot.get_channel(draft_chat_channel_id)
        if not draft_chat_channel:
            print(f"Draft chat channel {draft_chat_channel_id} not found.")
            return

        # Send the content of the original message to the draft chat channel
        content = original_message.content
        embeds = original_message.embeds
        attachments = original_message.attachments
        files = [await attachment.to_file() for attachment in attachments]  # Convert attachments to files

        # Send content, embeds, and attachments if they exist
        if content or embeds or files:
            await draft_chat_channel.send(content=content, embeds=embeds, files=files)
        else:
            # If the original message has no content, embeds, or attachments, send a placeholder
            await draft_chat_channel.send("Moved message content not available.")

        # Delete the original message after a delay
        await asyncio.sleep(10)  # Wait for 10 seconds before deleting the message
        await original_message.delete()

class PersistentView(View):
    def __init__(self, session_id):
        super().__init__(timeout=None)
        self.session_id = session_id

        # Initialize buttons without directly setting callbacks via decorators
        self.add_item(discord.ui.Button(label="Sign Up", style=discord.ButtonStyle.green, custom_id=f"{session_id}_sign_up"))
        self.add_item(discord.ui.Button(label="Cancel Sign Up", style=discord.ButtonStyle.red, custom_id=f"{session_id}_cancel_sign_up"))
        self.add_item(discord.ui.Button(label="Cancel Draft", style=discord.ButtonStyle.grey, custom_id=f"{session_id}_cancel_draft"))
        self.add_item(discord.ui.Button(label="Randomize Teams", style=discord.ButtonStyle.blurple, custom_id=f"{session_id}_randomize_teams"))
        self.add_item(discord.ui.Button(label="Create Chat Rooms", style=discord.ButtonStyle.green, custom_id=f"{session_id}_draft_complete", disabled=True))
        self.add_item(discord.ui.Button(label="Post Pairings", style=discord.ButtonStyle.primary, custom_id=f"{session_id}_post_pairings", disabled=True))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.data['custom_id'] == f"{self.session_id}_sign_up":
            await self.sign_up_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_cancel_sign_up":
            await self.cancel_sign_up_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_cancel_draft":
            await self.cancel_draft_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_randomize_teams":
            await self.randomize_teams_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_draft_complete":
            await self.draft_complete_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_post_pairings":
            await self.post_pairings_callback(interaction)
        else:
            # If the custom_id doesn't match any known button, you may want to log this or handle it appropriately.
            # Returning False will stop the interaction from being processed further by this View.
            return False

        # Returning True to indicate the interaction has been successfully processed.
        return True

    async def sign_up_callback(self, interaction: discord.Interaction): 
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return

        user_id = interaction.user.id
        if user_id in session.sign_ups:
            # User is already signed up; inform them
            await interaction.response.send_message("You are already signed up!", ephemeral=True)
        else:
            # User is signing up
            session.sign_ups[user_id] = interaction.user.display_name
            # Confirm signup
            await interaction.response.send_message("You are now signed up.", ephemeral=True)
            # Update the draft message to reflect the new list of sign-ups
            await session.update_draft_message(interaction)
        

    async def cancel_sign_up_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return

        user_id = interaction.user.id
        if user_id not in session.sign_ups:
            # User is not signed up; inform them
            await interaction.response.send_message("You are not signed up!", ephemeral=True)
        else:
            # User is canceling their sign-up
            del session.sign_ups[user_id]
            await interaction.response.send_message("Your sign-up has been canceled.", ephemeral=True)
            # Update the draft message to reflect the change in sign-ups
            await session.update_draft_message(interaction)
        

    async def draft_complete_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild

        # Assuming team_a and team_b are lists of member IDs
        team_a_members = [guild.get_member(user_id) for user_id in session.team_a]
        team_b_members = [guild.get_member(user_id) for user_id in session.team_b]
        all_members = team_a_members + team_b_members

        team_a_members = [member for member in team_a_members if member]  # Filter out None
        team_b_members = [member for member in team_b_members if member]  # Filter out None

        # Correctly pass team_a and team_b IDs to the method
        tasks = [
            session.create_team_channel(guild, "Team-A", team_a_members, session.team_a, session.team_b),
            session.create_team_channel(guild, "Team-B", team_b_members, session.team_a, session.team_b),
            session.create_team_channel(guild, "Draft-chat", all_members, session.team_a, session.team_b)  # Assuming you want overseers in draft chat too
        ]
        await asyncio.gather(*tasks)

        # No changes needed here if update_draft_complete_message does not require modification
        await session.update_draft_complete_message(interaction)
        
    #this implementation needs work. Right now it removes a user if they are in the session, but does not cancel it.
    #it only cancels if no one remains in session. Maybe thats better?
    async def cancel_draft_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return

        user_id = interaction.user.id
        # Check if the user is in session.sign_ups or if session.sign_ups is empty
        if user_id in session.sign_ups or not session.sign_ups:
            # Perform cancellation
            if user_id in session.sign_ups:
                del session.sign_ups[user_id]
                await interaction.response.send_message("Your sign-up has been canceled.", ephemeral=True)
                # Optionally, update the draft message to reflect the change in sign-ups
                await session.update_draft_message(interaction)
            if not session.sign_ups:
                #if no more signups, delete the draft session
                await interaction.message.delete()
                sessions.pop(self.session_id, None)
                await interaction.response.send_message("The draft has been canceled.", ephemeral=True)
        else:
            # If the user is not signed up and there are sign-ups present, do not allow cancellation
            await interaction.response.send_message("You cannot cancel this draft because you are not signed up or there are active sign-ups.", ephemeral=True)

        

    async def randomize_teams_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return

        if session.team_a is None or session.team_b is None:
            session.split_into_teams()

        # Generate names for display using the session's sign_ups dictionary
        team_a_display_names = [session.sign_ups[user_id] for user_id in session.team_a]
        team_b_display_names = [session.sign_ups[user_id] for user_id in session.team_b]
        seating_order = await session.generate_seating_order()

        # Create the embed message for displaying the teams and seating order
        embed = discord.Embed(
            title="Draft is Ready!",
            description=f"**Draftmancer Session**: **[Join Here]({session.draft_link})**" +
                        "\n\nNote: Host of Draftmancer must manually adjust seating as per below",
            color=discord.Color.blue()
        )
        embed.add_field(name="Team A", value="\n".join(team_a_display_names), inline=True)
        embed.add_field(name="Team B", value="\n".join(team_b_display_names), inline=True)
        embed.add_field(name="Seating Order", value=" -> ".join(seating_order), inline=False)


        # Iterate over the view's children (buttons) to update their disabled status
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                # Enable "Post Pairings" and "Draft Complete" buttons
                if item.custom_id in [f"{self.session_id}_post_pairings", f"{self.session_id}_draft_complete"]:
                    item.disabled = False
                else:
                    # Disable all other buttons
                    item.disabled = True

        # Respond with the embed and updated view
        await interaction.response.edit_message(embed=embed, view=self)

        
    
    async def post_pairings_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()  # Ensure there's enough time for operations

        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return

        original_message_id = session.message_id
        original_channel_id = interaction.channel.id  
        draft_chat_channel_id = session.draft_chat_channel
        await session.move_message_to_draft_channel(bot, original_channel_id, original_message_id, draft_chat_channel_id)

        # Use the existing team_a and team_b for pairings
        pairings = session.calculate_pairings()

        # Post pairings in the draft chat channel
        await session.post_pairings(interaction.guild, pairings)

        await interaction.followup.send("Pairings have been posted to the draft chat channel and the original message moved.", ephemeral=True)
    
    async def sign_up(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.sign_up_callback(interaction, interaction.user.id)

    async def cancel_sign_up(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.cancel_sign_up_callback(interaction, interaction.user.id)
        
    async def draft_complete(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.draft_complete_callback(interaction)

    async def cancel_draft(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.cancel_draft_callback(interaction)

    async def randomize_teams(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.randomize_teams_callback(interaction)

    async def post_pairings(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.post_pairings_callback(interaction)


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}!')

@bot.slash_command(name='startdraft', description='Start a Magic: The Gathering draft table', guild_id=None)
async def start_draft(interaction: discord.Interaction):
    await interaction.response.defer()

    draft_start_time = datetime.now().timestamp()
    session_id = f"{interaction.user.id}-{int(draft_start_time)}"
    draft_id = ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
    draft_link = f"https://draftmancer.com/?session=DB{draft_id}"

    session = DraftSession(session_id)
    session.guild_id = interaction.guild_id
    session.draft_link = draft_link
    session.draft_id = draft_id
    session.draft_start_time = draft_start_time

    sessions[session_id] = session

    cube_drafter_role = discord.utils.get(interaction.guild.roles, name="Cube Drafter")
    ping_message = f"{cube_drafter_role.mention if cube_drafter_role else 'Cube Drafter'} Vintage Cube Draft Queue Open!"
    await interaction.followup.send(ping_message, ephemeral=False)

    embed = discord.Embed(
        title=f"Vintage Cube Team Draft Queue - Started <t:{int(draft_start_time)}:R>",
        description=f"\n**Click Sign Up to Join!** \n\nNote: Draftmancer settings, such as importing the cube, must still be managed by the host. Seating order will be determined in the next step.\n\n**Draftmancer Session**: **[Join Here]({draft_link})**",
        color=discord.Color.dark_magenta()
    )
    embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)
    embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1186757246936424558/1217295353972527176/131.png")

    view = PersistentView(session_id)
  
    message = await interaction.followup.send(embed=embed, view=view)
    print(f"Session {session_id} has been created.")
    session.draft_message_id = message.id
    session.message_id = message.id
    # Pin the message to the channel
    await message.pin()

async def cleanup_sessions_task():
    while True:
        current_time = datetime.now()
        for session_id, session in list(sessions.items()):  # Use list to avoid RuntimeError due to size change during iteration
            if current_time >= session.deletion_time:
                # Attempt to delete each channel associated with the session
                for channel_id in session.channel_ids:
                    channel = bot.get_channel(channel_id)
                    if channel:  # Check if channel was found
                        try:
                            await channel.delete(reason="Session expired.")
                            print(f"Deleted channel: {channel.name}")
                        except discord.HTTPException as e:
                            print(f"Failed to delete channel: {channel.name}. Reason: {e}")
                
                # Once all associated channels are handled, remove the session from the dictionary
                del sessions[session_id]
                print(f"Session {session_id} has been removed.")

        # run function every hour
        await asyncio.sleep(3600)  # Sleep for 1 hour

bot.run(TOKEN)
