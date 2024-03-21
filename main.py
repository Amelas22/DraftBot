import discord
import asyncio
import os
import dotenv
from datetime import datetime, timedelta
from discord.ext import commands
from discord.ui import Select, View
from discord import SelectOption
import random
import secrets
import json

# Load the environment variables
dotenv.load_dotenv()

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True

TOKEN = os.getenv("BOT_TOKEN")

is_cleanup_task_running = False
is_periodic_save_sessions_running = False

bot = commands.Bot(command_prefix="!", intents=intents)

sessions = {}

class DraftSession:
    def __init__(self, session_id):
        self.session_id = session_id
        self.message_id = None
        self.draft_channel_id = None
        self.draft_message_id = None
        self.ready_check_message_id = None
        self.draft_link = None
        self.ready_check_status = {"ready": [], "not_ready": [], "no_response": []}  # Track users' ready status
        self.draft_start_time = datetime.now()
        self.deletion_time = datetime.now() + timedelta(hours=5)
        self.teams_start_time = None
        self.draft_chat_channel = None
        self.guild_id = None
        self.draft_id = None
        self.pairings = {}
        self.team_a = []
        self.team_b = []
        self.victory_message_id = None
        self.draft_summary_message_id = None
        self.matches = {}  
        self.match_results = {}
        self.match_counter = 1  
        self.sign_ups = {}
        self.channel_ids = []
        self.session_type = None
        self.session_stage = None
        self.team_a_name = None
        self.team_b_name = None

    async def update_draft_message(self, interaction):
        message = await interaction.channel.fetch_message(self.message_id)
        embed = message.embeds[0]
        sign_ups_count = len(self.sign_ups)
        sign_ups_field_name = f"Sign-Ups ({sign_ups_count}):" if self.sign_ups else "Sign-Ups (0):"
        sign_ups_str = '\n'.join(self.sign_ups.values()) if self.sign_ups else 'No players yet.'
        embed.set_field_at(0, name=sign_ups_field_name, value=sign_ups_str, inline=False)

        await message.edit(embed=embed)

    async def handle_ready_interaction(self, user_id: int):
        # If user is in 'not_ready', remove them from there
        if user_id in self.ready_check_status["not_ready"]:
            self.ready_check_status["not_ready"].remove(user_id)
        # Only add to 'ready' if they're not already there
        if user_id not in self.ready_check_status["ready"]:
            self.ready_check_status["ready"].append(user_id)
        # Remove from 'no_response' regardless
        if user_id in self.ready_check_status["no_response"]:
            self.ready_check_status["no_response"].remove(user_id)

    async def handle_not_ready_interaction(self, user_id: int):
        # If user is in 'ready', remove them from there
        if user_id in self.ready_check_status["ready"]:
            self.ready_check_status["ready"].remove(user_id)
        # Only add to 'not_ready' if they're not already there
        if user_id not in self.ready_check_status["not_ready"]:
            self.ready_check_status["not_ready"].append(user_id)
        # Remove from 'no_response' regardless
        if user_id in self.ready_check_status["no_response"]:
            self.ready_check_status["no_response"].remove(user_id)

    async def update_ready_check_message(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Ready Check Initiated",
                              description="Please indicate if you are ready. \nClick a name to open a DM if you're waiting on a response",
                              color=discord.Color.gold())
        embed.add_field(name="Ready", value="\n".join([interaction.guild.get_member(user_id).mention for user_id in self.ready_check_status["ready"]]), inline=False)
        embed.add_field(name="Not Ready", value="\n".join([interaction.guild.get_member(user_id).mention for user_id in self.ready_check_status["not_ready"]]), inline=False)
        embed.add_field(name="No Response", value="\n".join([interaction.guild.get_member(user_id).mention for user_id in self.ready_check_status["no_response"]]), inline=False)

        message = await interaction.channel.fetch_message(self.ready_check_message_id)
        await message.edit(embed=embed)

    async def initiate_ready_check(self, interaction: discord.Interaction):
        # Initialize all signed-up users as "no_response"
        self.ready_check_status["no_response"] = list(self.sign_ups.keys())
        await interaction.response.defer()

        # Create the initial ready check embed
        embed = discord.Embed(title="Ready Check Initiated",
                            description="Please indicate if you are ready.",
                            color=discord.Color.gold())
        embed.add_field(name="Ready", value="None", inline=False)
        embed.add_field(name="Not Ready", value="None", inline=False)
        embed.add_field(name="No Response", value="\n".join([interaction.guild.get_member(user_id).mention for user_id in self.sign_ups if user_id in self.ready_check_status["no_response"]]), inline=False)

        # Use the ReadyCheckView for managing button interactions
        view = self.ReadyCheckView(self.session_id)

        # Send the message as a follow-up to the interaction
        message = await interaction.followup.send(embed=embed, view=view)
        self.ready_check_message_id = message.id
        sign_up_tags = ' '.join([interaction.guild.get_member(user_id).mention for user_id in self.sign_ups.keys()])

        # Send a separate follow-up message to tag all signed-up users
        await interaction.followup.send(f"A Ready Check has been called! Make sure you are in the Draftmancer lobby. {sign_up_tags}")
        

    class ReadyCheckView(discord.ui.View):
        def __init__(self, session_id, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.session_id = session_id

        @discord.ui.button(label="Ready", style=discord.ButtonStyle.green, custom_id="ready_check_ready")
        async def ready_button(self, button: discord.ui.Button, interaction: discord.Interaction):
            session = sessions.get(self.session_id)
            if session:
                await session.handle_ready_interaction(interaction.user.id)
                await session.update_ready_check_message(interaction)
                await interaction.response.edit_message(view=self)

        @discord.ui.button(label="Not Ready", style=discord.ButtonStyle.red, custom_id="ready_check_not_ready")
        async def not_ready_button(self, button: discord.ui.Button, interaction: discord.Interaction):
            session = sessions.get(self.session_id)
            if session:
                await session.handle_not_ready_interaction(interaction.user.id)
                await session.update_ready_check_message(interaction)
                await interaction.response.edit_message(view=self)

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

        if team_name in ["Team-A", "Team-B"]:
            # Add all overseers with read permission initially, if it's a team-specific channel
            if overseer_role:
                for overseer in overseer_role.members:
                    # Check if the overseer is part of the current team or not
                    if overseer.id not in team_a and overseer.id not in team_b:
                        overwrites[overseer] = discord.PermissionOverwrite(read_messages=True)
                    elif (team_name == "Team-A" and overseer.id in team_b) or (team_name == "Team-B" and overseer.id in team_a):
                        # Remove access for overseers who are part of the other team
                        overwrites[overseer] = discord.PermissionOverwrite(read_messages=False)
        else:
            # For the "Draft-chat" channel, add all overseers
            if overseer_role:
                overwrites[overseer_role] = discord.PermissionOverwrite(read_messages=True)

        # Add team members with read permission. This specifically allows these members, overriding role-based permissions if needed.
        for member in team_members:
            overwrites[member] = discord.PermissionOverwrite(read_messages=True)
        
        # Create the channel with the specified overwrites
        channel = await guild.create_text_channel(name=channel_name, overwrites=overwrites, category=draft_category)
        self.channel_ids.append(channel.id)

        if team_name == "Draft-chat":
            self.draft_chat_channel = channel.id

           
    async def update_draft_complete_message(self, interaction):
        await interaction.followup.send("Channels created. You can now post pairings. Press only once; This process takes about 10 seconds to finish.", ephemeral=True)
        
    async def post_pairings(self, guild, pairings):
        if not self.draft_chat_channel:
            print("Draft chat channel not set.")
            return

        draft_chat_channel_obj = guild.get_channel(self.draft_chat_channel)
        if not draft_chat_channel_obj:
            print("Draft chat channel not found.")
            return

        await draft_chat_channel_obj.edit(slowmode_delay=0)

        for round_number, round_pairings in pairings.items():
            embed = discord.Embed(title=f"Round {round_number} Pairings", color=discord.Color.blue())
            view = self.create_pairings_view(round_pairings)  # Persistent view

            for player_id, opponent_id, match_number in round_pairings:
                player = guild.get_member(player_id)
                opponent = guild.get_member(opponent_id)
                player_name = player.display_name if player else 'Unknown'
                opponent_name = opponent.display_name if opponent else 'Unknown'

                # Formatting the pairings without wins
                match_info = f"**Match {match_number}**\n{player_name}\n{opponent_name}"
                embed.add_field(name="\u200b", value=match_info, inline=False)

            pairings_message = await draft_chat_channel_obj.send(embed=embed, view=view)
            # Store the message ID with the round and match number for later reference
            for _, _, match_number in round_pairings:
                self.matches[match_number]['message_id'] = pairings_message.id


    def create_pairings_view(self, round_pairings):
        view = discord.ui.View(timeout=None)  # Persistent view
        for player_id, opponent_id, match_number in round_pairings:
            match_details = self.match_results.get(match_number, {})
            button_style = discord.ButtonStyle.grey if match_details.get('winner_id') is not None else discord.ButtonStyle.primary
            button = self.MatchResultButton(session_id=self.session_id, match_number=match_number, style=button_style, label=f"Match {match_number} Results")
            view.add_item(button)

        return view
    
    def calculate_pairings(self):
        num_players = len(self.team_a) + len(self.team_b)
        if num_players not in [6, 8]:
            raise ValueError("Unsupported number of players. Only 6 or 8 players are supported.")

        assert len(self.team_a) == len(self.team_b), "Teams must be of equal size."
        
        self.match_results = {}  
        pairings = {1: [], 2: [], 3: []}

         # Generate pairings
        for round in range(1, 4):
            round_pairings = []
            for i, player_a in enumerate(self.team_a):
                player_b_index = (i + round - 1) % len(self.team_b)
                player_b = self.team_b[player_b_index]

                match_number = self.match_counter
                self.matches[match_number] = {"players": (player_a, player_b), "results": None}
                self.match_results[match_number] = {
                    "player1_id": player_a, "player1_wins": 0, 
                    "player2_id": player_b, "player2_wins": 0,
                    "winner_id": None  # Initialize with no winner
                }
                
                round_pairings.append((player_a, player_b, match_number))
                self.match_counter += 1

            pairings[round] = round_pairings

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

        random.shuffle(team_a_members)
        random.shuffle(team_b_members)

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

        # Use the generate_draft_summary_embed method to create the embed for the summary
        summary_embed = self.generate_draft_summary_embed()

        # Send the draft summary message to the draft chat channel
        summary_message = await draft_chat_channel.send(embed=summary_embed)
        self.draft_summary_message_id = summary_message.id  # Store the message ID for later updates

        await original_message.delete()
        await summary_message.pin()

    
    async def update_draft_summary(self):
        if not hasattr(self, 'draft_summary_message_id') or not self.draft_summary_message_id:
            print("Draft summary message ID not set.")
            return

        guild = bot.get_guild(self.guild_id)  # Directly use the global `bot` instance
        if not guild:
            print("Guild not found.")
            return

        channel = guild.get_channel(self.draft_chat_channel)
        if channel:
            try:
                summary_message = await channel.fetch_message(self.draft_summary_message_id)
                new_embed = self.generate_draft_summary_embed()  # Generate a new embed with updated results
                await summary_message.edit(embed=new_embed)
            except Exception as e:
                print(f"Failed to update draft summary message: {e}")
        else:
            print("Draft chat channel not found.")

    async def check_and_post_victory_or_draw(self):
        guild = bot.get_guild(self.guild_id)
        if not guild:
            print("Guild not found.")
            return

        team_a_wins, team_b_wins = self.calculate_team_wins()
        total_matches = len(self.match_results)
        half_matches = total_matches // 2
        #Check victory or draw conditions
        if team_a_wins > half_matches or team_b_wins > half_matches or (team_a_wins == half_matches and team_b_wins == half_matches and total_matches % 2 == 0):
            # Generate the victory or draw embed
            embed = self.generate_draft_summary_embed()
            three_zero_drafters = self.calculate_three_zero_drafters(guild)
            embed.add_field(name="3-0 Drafters", value=three_zero_drafters or "None", inline=False)

            # Post or update in the draft-chat channel
            draft_chat_channel = guild.get_channel(self.draft_chat_channel)
            await self.post_or_update_victory_message(draft_chat_channel, embed, 'victory_message_id_draft_chat')

            # Post or update in the team-draft-results channel
            results_channel = discord.utils.get(guild.text_channels, name="team-draft-results") if self.session_type == "random" else discord.utils.get(guild.text_channels, name="league-draft-results")
            await self.post_or_update_victory_message(results_channel, embed, 'victory_message_id_results_channel')

    async def post_or_update_victory_message(self, channel, embed, victory_message_attr):
        if not channel:
            print(f"Channel not found: {channel}")
            return

        # Check if the message ID attribute exists and post or update the message accordingly
        if not hasattr(self, victory_message_attr) or getattr(self, victory_message_attr) is None:
            message = await channel.send(embed=embed)
            setattr(self, victory_message_attr, message.id)  # Store the ID of the newly posted message
        else:
            try:
                message_id = getattr(self, victory_message_attr)
                message = await channel.fetch_message(message_id)
                await message.edit(embed=embed)  # Edit the existing message with the updated embed
            except discord.NotFound:
                print(f"Victory message with ID {message_id} not found in {channel.name}.")
            except Exception as e:
                print(f"Failed to update victory message in {channel.name}: {e}")

    
    def generate_draft_summary_embed(self):
        guild = bot.get_guild(self.guild_id)
        if not guild:
            return None

        team_a_wins, team_b_wins = self.calculate_team_wins()
        total_matches = len(self.match_results)
        half_matches = total_matches // 2

        # Determine the title and description based on the winning conditions
        title, description = self.determine_draft_outcome(team_a_wins, team_b_wins, half_matches, total_matches)

        # Prepare the embed
        embed = discord.Embed(title=title, description=description, color=discord.Color.yellow())
        embed.add_field(name="Team A" if self.session_type == "random" else f"{self.team_a_name}", value="\n".join([guild.get_member(user_id).display_name for user_id in self.team_a]), inline=True)
        embed.add_field(name="Team B" if self.session_type == "random" else f"{self.team_b_name}", value="\n".join([guild.get_member(user_id).display_name for user_id in self.team_b]), inline=True)
        embed.add_field(name="**Draft Standings**", value=f"**Team A Wins:** {team_a_wins}\n**Team B Wins:** {team_b_wins}", inline=False)

        return embed

    def calculate_three_zero_drafters(self, guild):
        # Count wins for each player
        win_counts = {}
        for match_details in self.match_results.values():
            winner_id = match_details.get("winner_id")
            if winner_id:
                win_counts[winner_id] = win_counts.get(winner_id, 0) + 1
        
        # Identify players with 3 wins
        three_zero_drafters = [player_id for player_id, win_count in win_counts.items() if win_count == 3]
        three_zero_names = ", ".join([guild.get_member(player_id).display_name for player_id in three_zero_drafters])
        return three_zero_names

    def determine_draft_outcome(self, team_a_wins, team_b_wins, half_matches, total_matches):
        guild = bot.get_guild(self.guild_id)
        if team_a_wins > half_matches or team_b_wins > half_matches:
            winner_team = self.team_a if team_a_wins > team_b_wins else self.team_b
            if self.session_type == "random":
                title = "Congratulations to " + ", ".join([guild.get_member(member_id).display_name for member_id in winner_team]) + " on winning the draft!"
                description = f"Draft Start: <t:{int(self.draft_start_time)}:F>"
            elif self.session_type == "premade":
                title = f"{self.team_a_name} has won the match!" if winner_team == self.team_a else f"{self.team_b_name} has won the match!"
                description = f"Congratulations to " + ", ".join([guild.get_member(member_id).display_name for member_id in winner_team]) + f" on winning the draft!\nDraft Start: <t:{int(self.draft_start_time)}:F>"
        elif team_a_wins == half_matches and team_b_wins == half_matches and total_matches % 2 == 0:
            title = "The Draft is a Draw!"
            description = "Great effort from both teams!"
        else:
            title = f"Draft-{self.draft_id} Standings"
            description = "If a drafter is missing from this channel, they likely can still see the channel but have the Discord invisible setting on."
        return title, description


    def create_updated_view_for_pairings_message(self, pairings_message_id):
        view = discord.ui.View(timeout=None)
        # Loop through all matches to reconstruct the view

        for match_id, details in self.matches.items():
            if details.get('message_id') == pairings_message_id:
                match_details = self.match_results.get(match_id, {})
                # Check if a winner has been reported for the match
                has_winner_reported = match_details.get('winner_id') is not None
                # Determine the button style: grey if a winner has been reported, otherwise primary
                button_style = discord.ButtonStyle.grey if has_winner_reported else discord.ButtonStyle.primary
                # Instantiate a new button with the determined style and the same match number
                button = self.MatchResultButton(self.session_id, match_id, style=button_style, label=f"Match {match_id} Results")
                view.add_item(button)
        return view
    
    async def update_team_view(self, interaction: discord.Interaction):
        # Fetch the message to be updated
        message = await interaction.channel.fetch_message(self.message_id)
        embed = message.embeds[0]  # Assuming there's only one embed attached to the message

        # Fetch member display names for each team
        guild = interaction.guild
        team_a_names = [guild.get_member(user_id).display_name for user_id in self.team_a]
        team_b_names = [guild.get_member(user_id).display_name for user_id in self.team_b]

        # Update the fields for Team A and Team B with new compositions and counts
        # Find the index of the Team A and Team B fields
        team_a_name = str(self.team_a_name)
        team_b_name = str(self.team_b_name)
        team_a_index = next((i for i, e in enumerate(embed.fields) if e.name.startswith(team_a_name)), None)
        team_b_index = next((i for i, e in enumerate(embed.fields) if e.name.startswith(team_b_name)), None)

        # Update the fields if found
        if team_a_index is not None:
            embed.set_field_at(team_a_index, name=f"{self.team_a_name} ({len(self.team_a)}):", value="\n".join(team_a_names) if team_a_names else "No players yet.", inline=False)
        if team_b_index is not None:
            embed.set_field_at(team_b_index, name=f"{self.team_b_name} ({len(self.team_b)}):", value="\n".join(team_b_names) if team_b_names else "No players yet.", inline=False)

        # Edit the original message with the updated embed
        await message.edit(embed=embed)

    async def update_pairings_posting(self, match_number):
        guild = bot.get_guild(self.guild_id)
        if not guild:
            print("Guild not found.")
            return

        match_details = self.match_results.get(match_number)
        if not match_details:
            print(f"Match details for match {match_number} not found.")
            return

        message_id = self.matches.get(match_number, {}).get('message_id')
        if not message_id:
            print(f"Pairings message ID for match {match_number} not found.")
            return

        channel = guild.get_channel(self.draft_chat_channel)
        if not channel:
            print("Pairings message channel not found.")
            return

        try:
            message = await channel.fetch_message(message_id)
            embed = message.embeds[0] if message.embeds else None
            if not embed:
                print("No embed found in pairings message.")
                return

            # Update the embed with the new match results
            for i, field in enumerate(embed.fields):
                if f"**Match {match_number}**" in field.value:
                    player1 = guild.get_member(match_details['player1_id'])
                    player2 = guild.get_member(match_details['player2_id'])
                    player1_wins = match_details['player1_wins'] or 0
                    player2_wins = match_details['player2_wins'] or 0
                    updated_value = f"**Match {match_number}**\n{player1.display_name}: {player1_wins} wins\n{player2.display_name}: {player2_wins} wins"
                    embed.set_field_at(i, name=field.name, value=updated_value, inline=field.inline)
                    break

            # Re-generate the view with potentially updated button styles
            new_view = self.create_updated_view_for_pairings_message(message_id)

            # Edit the message with the updated embed and view
            await message.edit(embed=embed, view=new_view)

        except discord.NotFound:
            print(f"Pairings message with ID {message_id} not found in channel.")
        except Exception as e:
            print(f"Failed to update pairings posting for match {match_number}: {e}")

    def calculate_team_wins(self):
        team_a_wins = 0
        team_b_wins = 0

        for match_id, match in self.match_results.items():
            player1_wins = match.get('player1_wins', 0) or 0  # Default to 0 if None
            player2_wins = match.get('player2_wins', 0) or 0  # Default to 0 if None

            if player1_wins > player2_wins:
                if match['player1_id'] in self.team_a:
                    team_a_wins += 1
                else:
                    team_b_wins += 1
            elif player2_wins > player1_wins:
                if match['player2_id'] in self.team_a:
                    team_a_wins += 1
                else:
                    team_b_wins += 1

        return team_a_wins, team_b_wins

    class MatchResultButton(discord.ui.Button):
        def __init__(self, session_id, match_number, style=discord.ButtonStyle.primary, label=None, disabled=False, **kwargs):
            # Ensure to call the superclass __init__ with appropriate keyword arguments
            super().__init__(style=style, label=label or f"Match {match_number} Results", disabled=disabled, **kwargs)
            self.session_id = session_id
            self.match_number = match_number
            
        async def callback(self, interaction: discord.Interaction):
            session = sessions.get(self.session_id)
            if session:
                guild = bot.get_guild(session.guild_id)  # Use bot instance to fetch guild
                if guild:
                    # Instantiate MatchResultSelect
                    select = DraftSession.MatchResultSelect(self.match_number, session, guild)
                    # Create a new View and add the Select to it
                    view = discord.ui.View()
                    view.add_item(select)
                    # Use the View in send_message
                    await interaction.response.send_message("Report results for Match.", view=view, ephemeral=True)
                else:
                    await interaction.response.send_message("Guild not found.", ephemeral=True)


    class MatchResultSelect(discord.ui.Select):
        def __init__(self, match_number, session, guild, *args, **kwargs):
            # Fetch the player IDs from the match results
            player1_id, player2_id = session.matches[match_number]['players']

            # Fetch player names using their IDs from the guild object
            player1_name = guild.get_member(player1_id).display_name if guild.get_member(player1_id) else "Player 1"
            player2_name = guild.get_member(player2_id).display_name if guild.get_member(player2_id) else "Player 2"

            options = [
                discord.SelectOption(label=f"{player1_name} wins: 2-0", value="2-0-1"),
                discord.SelectOption(label=f"{player1_name} wins: 2-1", value="2-1-1"),
                discord.SelectOption(label=f"{player2_name} wins: 2-0", value="0-2-2"),
                discord.SelectOption(label=f"{player2_name} wins: 2-1", value="1-2-2"),
                discord.SelectOption(label="No Match Played", value="0-0-0"),
            ]
            super().__init__(placeholder=f"{player1_name} v. {player2_name}", min_values=1, max_values=1, options=options, custom_id=f"result_select_{match_number}")
            self.match_number = match_number
            self.session = session
            self.guild = guild

        async def callback(self, interaction: discord.Interaction):
            player1_wins, player2_wins, winner = self.values[0].split('-')
            player1_wins = int(player1_wins)
            player2_wins = int(player2_wins)
            winner = int(winner)

            match_result = self.session.match_results.get(self.match_number)
            if match_result:
                match_result['player1_wins'] = player1_wins
                match_result['player2_wins'] = player2_wins
                if winner != 0:  # Update the winner_id if a winner is determined
                    winner_id = match_result['player1_id'] if winner == 1 else match_result['player2_id']
                    match_result['winner_id'] = winner_id

                message = f"Match result recorded: {player1_wins}-{player2_wins}"
                await interaction.response.send_message(message, ephemeral=True)
                save_sessions_to_file(sessions)
                await self.session.check_and_post_victory_or_draw() #check winner
                await self.session.update_draft_summary()  # Update the draft summary 
                await self.session.update_pairings_posting(self.match_number)  # Update the pairings message
            else:
                await interaction.response.send_message("Error: Match result could not be found.", ephemeral=True)

    class ResultReportView(discord.ui.View):
        def __init__(self, match_number, session, guild):  # Accept guild as a parameter
            super().__init__(timeout=180)
            self.match_number = match_number
            self.session = session
            self.guild = guild  # Store guild object

            # Remove the WinSelect items and replace them with a single MatchResultSelect item
            self.add_item(DraftSession.MatchResultSelect(match_number, session))

    def to_dict(self):
        # Convert all attributes to a dictionary, except for those that need special handling
        session_dict = {k: v for k, v in self.__dict__.items() if not k.startswith('_') and not callable(v) and not isinstance(v, datetime)}

        # Manually convert datetime objects to ISO format strings
        if isinstance(self.draft_start_time, datetime):
            session_dict['draft_start_time'] = self.draft_start_time.isoformat()
        if isinstance(self.deletion_time, datetime):
            session_dict['deletion_time'] = self.deletion_time.isoformat()

        return session_dict

    def update_from_dict(self, session_dict):
        """
        Update the session instance based on a dictionary.
        This is intended for use when loading session data from JSON.
        """
        for key, value in session_dict.items():
            if key in ['draft_start_time', 'deletion_time'] and isinstance(value, str):
                # Convert from ISO format string to datetime
                setattr(self, key, datetime.fromisoformat(value))
            else:
                setattr(self, key, value)


class PersistentView(View):
    def __init__(self, session_id):
        super().__init__(timeout=None)
        self.session_id = session_id
        session = sessions.get(session_id)

        
        if session.session_type == 'premade':
            self.add_item(discord.ui.Button(label=f"{session.team_a_name}", style=discord.ButtonStyle.green, custom_id=f"{session_id}_Team_A"))
            self.add_item(discord.ui.Button(label=f"{session.team_b_name}", style=discord.ButtonStyle.red, custom_id=f"{session_id}_Team_B"))
            self.add_item(discord.ui.Button(label="Generate Seating Order", style=discord.ButtonStyle.blurple, custom_id=f"{session_id}_generate_seating"))
        elif session.session_type == 'random':
            self.add_item(discord.ui.Button(label="Sign Up", style=discord.ButtonStyle.green, custom_id=f"{session_id}_sign_up"))
            self.add_item(discord.ui.Button(label="Cancel Sign Up", style=discord.ButtonStyle.red, custom_id=f"{session_id}_cancel_sign_up"))
            self.add_item(discord.ui.Button(label="Create Teams", style=discord.ButtonStyle.blurple, custom_id=f"{session_id}_randomize_teams"))
                
        self.add_item(discord.ui.Button(label="Cancel Draft", style=discord.ButtonStyle.grey, custom_id=f"{session_id}_cancel_draft"))
        self.add_item(discord.ui.Button(label="Remove User", style=discord.ButtonStyle.grey, custom_id=f"{session_id}_remove_user"))
        self.add_item(discord.ui.Button(label="Ready Check", style=discord.ButtonStyle.green, custom_id=f"{session_id}_ready_check"))
        self.add_item(discord.ui.Button(label="Create Rooms & Post Pairings", style=discord.ButtonStyle.primary, custom_id=f"{session_id}_create_rooms_pairings", disabled=True))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        session = sessions.get(self.session_id)
        if interaction.data['custom_id'] == f"{self.session_id}_sign_up":
            await self.sign_up_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_cancel_sign_up":
            await self.cancel_sign_up_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_cancel_draft":
            await self.cancel_draft_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_randomize_teams":
            await self.randomize_teams_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_ready_check":
            await self.ready_check_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_Team_A":
            await self.team_assignment_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_Team_B":
            await self.team_assignment_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_generate_seating":
            await self.randomize_teams_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_create_rooms_pairings":
            await self.create_rooms_pairings_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_remove_user":
            await self.remove_user_button_callback(interaction)
            return False
        else:
            return False

        return True

    async def create_rooms_pairings_callback(self, interaction: discord.Interaction):
        # Defer the response to ensure there's enough time for operations
        await interaction.response.defer()

        session = sessions.get(self.session_id)
        if not session:
            await interaction.followup.send("The draft session could not be found.", ephemeral=True)
            return
        session.session_stage = 'pairings'

        guild = interaction.guild

        # Immediately disable the "Create Rooms & Post Pairings" button to prevent multiple presses
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label == "Create Rooms & Post Pairings":
                child.disabled = True
                break

        await interaction.edit_original_response(view=self)  # Now correctly referring to 'self'

        # Execute tasks to create chat channels
        team_a_members = [guild.get_member(user_id) for user_id in session.team_a]
        team_b_members = [guild.get_member(user_id) for user_id in session.team_b]
        all_members = team_a_members + team_b_members

        tasks = [
            session.create_team_channel(guild, "Draft-chat", all_members, session.team_a, session.team_b), 
            session.create_team_channel(guild, "Team-A", team_a_members, session.team_a, session.team_b),
            session.create_team_channel(guild, "Team-B", team_b_members, session.team_a, session.team_b)
        ]
        await asyncio.gather(*tasks)
        draft_chat_channel_id = session.draft_chat_channel
        # Post a sign-up ping in the draft chat channel
        draft_chat_channel = guild.get_channel(draft_chat_channel_id)
        if draft_chat_channel:
            sign_up_tags = ' '.join([f"<@{user_id}>" for user_id in session.sign_ups.keys()])
            await draft_chat_channel.send(f"Pairing posted below. Good luck in your matches! {sign_up_tags}")

        original_message_id = session.message_id
        original_channel_id = interaction.channel.id  
        session.pairings = session.calculate_pairings()
        await session.move_message_to_draft_channel(bot, original_channel_id, original_message_id, draft_chat_channel_id)
    
        # Execute Post Pairings

        await session.post_pairings(guild, session.pairings)


    async def sign_up_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return
        
        # Check if the sign-up list is already full
        if len(session.sign_ups) >= 8:
            await interaction.response.send_message("The sign-up list is already full. No more players can sign up.", ephemeral=True)
            return

        user_id = interaction.user.id
        if user_id in session.sign_ups:
            # User is already signed up; inform them
            await interaction.response.send_message("You are already signed up!", ephemeral=True)
        else:
            # User is signing up
            session.sign_ups[user_id] = interaction.user.display_name
            # Confirm signup with draft link
            draft_link = session.draft_link  # Ensure you have the draft_link available in your session
            signup_confirmation_message = f"You are now signed up. Join Here: {draft_link}"
            await interaction.response.send_message(signup_confirmation_message, ephemeral=True)
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
        
    
    async def ready_check_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if session:
            # Check if the user is in the sign-up list
            if interaction.user.id in session.sign_ups:
                # Proceed with the ready check
                await session.initiate_ready_check(interaction)

                # Disable the "Ready Check" button after use
                for item in self.children:
                    if isinstance(item, discord.ui.Button) and item.custom_id == f"{self.session_id}_ready_check":
                        item.disabled = True
                        break  # Stop the loop once the button is found and modified

                # Ensure the view reflects the updated state with the button disabled
                await interaction.edit_original_response(view=self)
            else:
                # Inform the user they're not in the sign-up list, hence can't initiate a ready check
                await interaction.response.send_message("You must be signed up to initiate a ready check.", ephemeral=True)
        else:
            await interaction.response.send_message("Session not found.", ephemeral=True)


    async def team_assignment_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("Session not found.", ephemeral=True)
            return

        user_id = interaction.user.id
        custom_id = interaction.data["custom_id"]
        user_name = interaction.user.display_name

        if "_Team_A" in custom_id:
            primary_team_key = "team_a"
            secondary_team_key = "team_b"
        elif "_Team_B" in custom_id:
            primary_team_key = "team_b"
            secondary_team_key = "team_a"
        else:
            await interaction.response.send_message("An error occurred.", ephemeral=True)
            return

        primary_team = getattr(session, primary_team_key, [])
        secondary_team = getattr(session, secondary_team_key, [])

        # Add or remove the user from the team lists
        if user_id in primary_team:
            primary_team.remove(user_id)
            del session.sign_ups[user_id]  # Remove from sign-ups dictionary
            action_message = f"You have been removed from a team."
        else:
            if user_id in secondary_team:
                secondary_team.remove(user_id)
                del session.sign_ups[user_id]  # Remove from sign-ups dictionary before re-adding to correct team
            primary_team.append(user_id)
            session.sign_ups[user_id] = user_name  # Add/update in sign-ups dictionary
            action_message = f"You have been added to a team."

        # Update session attribute to reflect changes
        setattr(session, primary_team_key, primary_team)
        setattr(session, secondary_team_key, secondary_team)

        await interaction.response.send_message(action_message, ephemeral=True)
        await session.update_team_view(interaction)

    

    async def cancel_draft_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return

        user_id = interaction.user.id
        # Check if the user is in session.sign_ups or if session.sign_ups is empty
        if user_id in session.sign_ups or not session.sign_ups:
            # Delete the draft message and remove the session
            await interaction.message.delete()
            sessions.pop(self.session_id, None)
            await interaction.response.send_message("The draft has been canceled.", ephemeral=True)
        else:
            # If the user is not signed up and there are sign-ups present, inform the user
            await interaction.response.send_message("You cannot cancel this draft because you are not signed up.", ephemeral=True)
    
    async def remove_user_button_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("Session not found.", ephemeral=True)
            return

        # Check if the user initiating the remove action is in the sign_ups
        if interaction.user.id not in session.sign_ups:
            await interaction.response.send_message("You are not authorized to remove users.", ephemeral=True)
            return

        # If the session exists and has sign-ups, and the user is authorized, proceed
        if session.sign_ups:
            options = [SelectOption(label=user_name, value=str(user_id)) for user_id, user_name in session.sign_ups.items()]
            view = UserRemovalView(session_id=self.session_id)
            await interaction.response.send_message("Select a user to remove:", view=view, ephemeral=True)
        else:
            await interaction.response.send_message("No users to remove.", ephemeral=True)

    async def randomize_teams_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return
        session.teams_start_time = datetime.now().timestamp()
        session.session_stage = 'teams'
        # Check session type and prepare teams if necessary
        if session.session_type == 'random':
            session.split_into_teams()

        # Generate names for display using the session's sign_ups dictionary
        team_a_display_names = [session.sign_ups[user_id] for user_id in session.team_a]
        team_b_display_names = [session.sign_ups[user_id] for user_id in session.team_b]
        
        seating_order = await session.generate_seating_order()

        # Create the embed message for displaying the teams and seating order
        embed = discord.Embed(
            title=f"Draft-{session.draft_id} is Ready!",
            description=f"**Draftmancer Session**: **[Join Here]({session.draft_link})** \n" +
                        "Host of Draftmancer must manually adjust seating as per below. **TURN OFF RANDOM SEATING SETTING IN DRAFMANCER**" +
                        "\n\n**AFTER THE DRAFT**, select Create Chat Rooms (give it five seconds to generate rooms) then select Post Pairings" +
                        "\nPost Pairings will take about 10 seconds to process. Only press once.",
            color=discord.Color.blue()
        )
        embed.add_field(name="Team A" if session.session_type == "random" else f"{session.team_a_name}", value="\n".join(team_a_display_names), inline=True)
        embed.add_field(name="Team B" if session.session_type == "random" else f"{session.team_b_name}", value="\n".join(team_b_display_names), inline=True)
        embed.add_field(name="Seating Order", value=" -> ".join(seating_order), inline=False)

        # Iterate over the view's children (buttons) to update their disabled status
        # Iterate over the view's children (buttons) to update their disabled status
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                # Enable "Create Rooms" and "Cancel Draft" buttons
                if item.custom_id == f"{self.session_id}_create_rooms_pairings" or item.custom_id == f"{self.session_id}_cancel_draft":
                    item.disabled = False
                else:
                    # Disable all other buttons
                    item.disabled = True


        # Respond with the embed and updated view
        await interaction.response.edit_message(embed=embed, view=self)
    

class UserRemovalSelect(Select):
    def __init__(self, options: list[SelectOption], session_id: str, *args, **kwargs):
        super().__init__(*args, **kwargs, placeholder="Choose a user to remove...", min_values=1, max_values=1, options=options)
        self.session_id = session_id

    async def callback(self, interaction: discord.Interaction):
        user_id_to_remove = int(self.values[0])
        session = sessions.get(self.session_id)

        if user_id_to_remove in session.sign_ups:
            removed_user_name = session.sign_ups.pop(user_id_to_remove)
            await interaction.response.send_message(f"Removed {removed_user_name} from the draft.", ephemeral=False)
            
            # After removing a user, update the original message with the new sign-up list
            await session.update_draft_message(interaction)

            await interaction.edit_original_response(content=f"Removed {removed_user_name} from the draft.", view=None)
        else:
            await interaction.response.send_message("User not found in sign-ups.", ephemeral=True)

class UserRemovalView(View):
    def __init__(self, session_id: str):
        super().__init__()
        session = sessions.get(session_id)
        if session and session.sign_ups:
            options = [SelectOption(label=user_name, value=str(user_id)) for user_id, user_name in session.sign_ups.items()]
            self.add_item(UserRemovalSelect(options=options, session_id=session_id))

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
    print(f'Logged in as {bot.user}! Loaded {len(sessions)} sessions.')


class CubeSelectionModal(discord.ui.Modal):
    def __init__(self, session_type, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_type = session_type
        self.add_item(discord.ui.InputText(label="Cube Name", placeholder="LSVCube, AlphaFrog, mtgovintage, or your choice", custom_id="cube_name_input"))
        if self.session_type == "premade":
            self.add_item(discord.ui.InputText(label="Team A Name", placeholder="Team A Name", custom_id="team_a_input"))
            self.add_item(discord.ui.InputText(label="Team B Name", placeholder="Team B Name", custom_id="team_b_input"))
        

    async def callback(self, interaction: discord.Interaction):
        cube_name_input = self.children[0]
        cube_name = cube_name_input.value
        cube_option = "MTG" if not cube_name else cube_name  # Default to "MTG" if no cube is selected or use the custom input

        draft_start_time = datetime.now().timestamp()
        session_id = f"{interaction.user.id}-{int(draft_start_time)}"
        draft_id = ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
        draft_link = f"https://draftmancer.com/?session=DB{draft_id}"

        # Create session object and set properties
        session = DraftSession(session_id)
        session.guild_id = interaction.guild_id
        session.draft_link = draft_link
        session.draft_id = draft_id
        session.draft_start_time = draft_start_time
        session.session_type = self.session_type

        add_session(session_id, session)

        await interaction.response.send_message("Setting up a draft...")
        if session.session_type == "random":
            cube_drafter_role = discord.utils.get(interaction.guild.roles, name="Cube Drafter")
            ping_message = f"{cube_drafter_role.mention if cube_drafter_role else 'Cube Drafter'} {cube_option} Cube Draft Queue Open!"
            await interaction.followup.send(ping_message)  # No need to mark as ephemeral=False, as it's the default behavior

            # Now create the embed with cube_option in the title
            embed_title = f"Looking for Players! {cube_option} Random Team Draft - Queue Opened <t:{int(draft_start_time)}:R>"
            embed = discord.Embed(title=embed_title,
            description="\n**How to use bot**:\n1. Click sign up and click the draftmancer link. Draftmancer host still has to update settings and import from CubeCobra.\n" +
                            "2. When enough people join (6 or 8), Push Ready Check. Once everyone is ready, push Create Teams\n" +
                            "3. Create Teams will create randoms teams and a corresponding seating order. Draftmancer host needs to adjust table to match seating order. **TURN OFF RANDOM SEATING IN DRAFTMANCER** \n" +
                            "4. After the draft, come back to this message (it'll be in pins) and click Create Rooms and Post Pairings. This will create a shared draft-chat and private team chats. Pairings will post in the draft-chat.\n" +
                            "5. After each match, you can select the Match Results buttons to report results. Once a winner is determined, it will be announced to the channel and to team-draft-results\n" +
                            "6. Chat channels will automatically close around five hours after the /startdraft command was used." +
                            f"\n\n**Chosen Cube**: {cube_option} \n**Draftmancer Session**: **[Join Here]({draft_link})**",
            color=discord.Color.dark_magenta()
        )
            embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)
            #thumbnail by chosen cube?
            embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1186757246936424558/1217295353972527176/131.png")
            print(f"Random Draft: {session_id} has been created.")
            
            
        elif session.session_type == "premade":
            team_a_input = self.children[1]
            team_b_input = self.children[2]
            team_a_name = team_a_input.value
            team_b_name = team_b_input.value
            team_a_option = "Team A" if not team_a_name else team_a_name
            team_b_option = "Team B" if not team_b_name else team_b_name
            embed = discord.Embed(
                title=f"{cube_option} Premade Team Draft Queue - Started <t:{int(draft_start_time)}:R>",
                description="\n**How to use bot**:\n1. Click Team A or Team B to join that team. Enter the draftmancer link. Draftmancer host still has to update settings and import from CubeCobra.\n" +
                                "2. When all teams are joined, Push Ready Check. Once everyone is ready, push Generate Seating Order\n" +
                                "3. Draftmancer host needs to adjust table to match seating order. **TURN OFF RANDOM SEATING IN DRAFTMANCER** \n" +
                                "4. After the draft, come back to this message (it'll be in pins) and push Create Rooms and Post Pairings.\n" +
                                "5. You will now have a private team chat with just your team and a shared draft-chat that has pairings and match results. You can select the Match Results buttons to report results.\n" +
                                "6. Chat channels will automatically close around five hours after the /startdraft command was used." +
                                f"\n\n**Draftmancer Session**: **[Join Here]({draft_link})**",
                color=discord.Color.blue()
            )
            embed.add_field(name=f"{team_a_option}", value="No players yet.", inline=False)
            embed.add_field(name=f"{team_b_option}", value="No players yet.", inline=False)
            embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1219018393471025242/1219410709440495746/image.png?ex=660b33b8&is=65f8beb8&hm=b7e40e9b872d8e04dd70a30c5abc15917379f9acb7dce74ca0372105ec98b468&")
            session.team_a_name = team_a_option
            session.team_b_name = team_b_option
            print(f"Premade Draft: {session_id} has been created.")



        view = PersistentView(session_id)
        message = await interaction.followup.send(embed=embed, view=view)
        session.draft_message_id = message.id
        session.message_id = message.id
        # Pin the message to the channel
        await message.pin()
    

@bot.slash_command(name='startdraft', description='Start a team draft with random teams', guild_id=None)
async def start_draft(interaction: discord.Interaction):
    await interaction.response.send_modal(CubeSelectionModal(session_type="random", title="Select Cube"))

    
    # session_id = f"{interaction.user.id}-{int(draft_start_time)}"
    # draft_id = ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
    # draft_link = f"https://draftmancer.com/?session=DB{draft_id}"

    # session = DraftSession(session_id)
    # session.guild_id = interaction.guild_id
    # session.draft_link = draft_link
    # session.draft_id = draft_id
    # session.draft_start_time = draft_start_time
    # session.session_type = "random"

    # add_session(session_id, session)

    # cube_drafter_role = discord.utils.get(interaction.guild.roles, name="Cube Drafter")
    # ping_message = f"{cube_drafter_role.mention if cube_drafter_role else 'Cube Drafter'} Vintage Cube Draft Queue Open!"
    # await interaction.followup.send(ping_message, ephemeral=False)

    # embed = discord.Embed(
    #     title=f"{embed_title}",
    #     description="\n**How to use bot**:\n1. Click sign up and click the draftmancer link. Draftmancer host still has to update settings and  from CubeCobra.\n" +
    #                     "2. When enough people join (6 or 8), Push Ready Check. Once everyone is ready, push Create Teams\n" +
    #                     "3. Create Teams will create randoms teams and a corresponding seating order. Draftmancer host needs to adjust table to match seating order. **TURN OFF RANDOM SEATING IN DRAFTMANCER** \n" +
    #                     "4. After the draft, come back to this message (it'll be in pins) and click Create Chat Rooms. After 5 seconds chat rooms will be ready and you can press Post Pairings. This takes 10 seconds to process.\n" +
    #                     "5. You will now have a private team chat with just your team and a shared draft chat that has pairings and match results. You can select the Match Results buttons to report results.\n" +
    #                     "6. Chat channels will automatically close around five hours after the /startdraft command was used." +
    #                     f"\n\n**Draftmancer Session**: **[Join Here]({draft_link})**",
    #     color=discord.Color.dark_magenta()
    # )
    # embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)
    # embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1186757246936424558/1217295353972527176/131.png")

    # view = PersistentView(session_id)
  
    # message = await interaction.followup.send(embed=embed, view=view)
    # print(f"Random Draft: {session_id} has been created.")
    # session.draft_message_id = message.id
    # session.message_id = message.id
    # # Pin the message to the channel
    # await message.pin()

@bot.slash_command(name='premadedraft', description='Start a team draft with premade teams', guild_id=None)
async def premade_draft(interaction: discord.Interaction):
    await interaction.response.send_modal(CubeSelectionModal(session_type="premade", title="Select Cube"))

    # draft_start_time = datetime.now().timestamp()
    # session_id = f"{interaction.user.id}-{int(draft_start_time)}"
    # draft_id = ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
    # draft_link = f"https://draftmancer.com/?session=DB{draft_id}"

    # session = DraftSession(session_id)
    # session.guild_id = interaction.guild_id
    # session.draft_link = draft_link
    # session.draft_id = draft_id
    # session.draft_start_time = draft_start_time
    # session.session_stage = 'sign-up'
    # session.session_type = "premade"

    # add_session(session_id, session)

    # embed = discord.Embed(
    #     title=f"MTG Premade Team Draft Queue - Started <t:{int(draft_start_time)}:R>",
    #     description="\n**How to use bot**:\n1. Click Team A or Team B to join that team. Enter the draftmancer link. Draftmancer host still has to update settings and  from CubeCobra.\n" +
    #                     "2. When all teams are joined, Push Ready Check. Once everyone is ready, push Generate Seating Order\n" +
    #                     "3. Draftmancer host needs to adjust table to match seating order. **TURN OFF RANDOM SEATING IN DRAFTMANCER** \n" +
    #                     "4. After the draft, come back to this message (it'll be in pins) and click Create Chat Rooms. After 5 seconds chat rooms will be ready and you can press Post Pairings. This takes 10 seconds to process.\n" +
    #                     "5. You will now have a private team chat with just your team and a shared draft chat that has pairings and match results. You can select the Match Results buttons to report results.\n" +
    #                     "6. Chat channels will automatically close around five hours after the /startdraft command was used." +
    #                     f"\n\n**Draftmancer Session**: **[Join Here]({draft_link})**",
    #     color=discord.Color.blue()
    # )
    # embed.add_field(name="Team A", value="No players yet.", inline=False)
    # embed.add_field(name="Team B", value="No players yet.", inline=False)
    # embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1219018393471025242/1219410709440495746/image.png?ex=660b33b8&is=65f8beb8&hm=b7e40e9b872d8e04dd70a30c5abc15917379f9acb7dce74ca0372105ec98b468&")
    # print(f"Premade Draft: {session_id} has been created.")

    # view = PersistentView(session_id)
    
    # message = await interaction.followup.send(embed=embed, view=view)
    
    # session.draft_message_id = message.id
    # session.message_id = message.id

def save_sessions_to_file(sessions, filename='sessions.json'):
    sessions_data = {session_id: session.to_dict() for session_id, session in sessions.items()}
    with open(filename, 'w') as f:
        json.dump(sessions_data, f, indent=4)

def load_sessions_from_file(filename='sessions.json'):
    try:
        with open(filename, 'r') as f:
            sessions_data = json.load(f)
        sessions = {}
        for session_id, session_dict in sessions_data.items():
            session = DraftSession.__new__(DraftSession)  # Create a new DraftSession instance without calling __init__
            session.session_id = session_id  # Manually set the session_id
            session.update_from_dict(session_dict)  # Update the instance based on the dictionary
            sessions[session_id] = session
        return sessions
    except FileNotFoundError:
        return {}  # Return an empty dictionary if the file doesn't exist



def add_session(session_id, session):
    global sessions  # Make sure to declare sessions as global if it's being accessed globally
    
    # Check if the sessions dictionary already contains 20 sessions
    if len(sessions) >= 20:
        # Sort sessions by the timestamp in their ID (assuming session_id format includes a timestamp) and remove the oldest
        oldest_session_id = sorted(sessions.keys(), key=lambda x: int(x.split('-')[-1]))[0]
        oldest_session = sessions.pop(oldest_session_id)
        # Delete associated chat channels if they still exist
        for channel_id in oldest_session.channel_ids:
            channel = bot.get_channel(channel_id)
            if channel:  # Check if channel was found and still exists
                asyncio.create_task(channel.delete(reason="Session expired due to session cap."))
                print(f"Deleting channel: {channel.name} for session {oldest_session_id}")

    # Add the new session
    sessions[session_id] = session
    save_sessions_to_file(sessions)  # Save sessions to file after adding a new session

async def periodic_save_sessions():
    while True:
        await asyncio.sleep(600)  # Wait for 10 minutes
        save_sessions_to_file(sessions)  # Assume this function saves your sessions to a file

async def cleanup_sessions_task():
    while True:
        current_time = datetime.now()
        for session_id, session in list(sessions.items()):  
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
                print(f"Session {session_id} has been removed due to time.")

        # run function every hour
        await asyncio.sleep(3600)  # Sleep for 1 hour

if __name__ == "__main__":
    bot.run(TOKEN)
