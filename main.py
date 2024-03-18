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
        self.ready_check_message_id = None
        self.draft_link = None
        self.ready_check_status = {"ready": [], "not_ready": [], "no_response": []}  # Track users' ready status
        self.draft_start_time = datetime.now()
        self.deletion_time = datetime.now() + timedelta(hours=5)
        self.draft_chat_channel = None
        self.guild_id = None
        self.draft_id = None
        self.pairings = {}
        self.team_a = None
        self.team_b = None
        self.draft_summary_message_id = None
        self.matches = {}  
        self.match_results = {}
        self.match_counter = 1  
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
                              description="Please indicate if you are ready.",
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

        # Send a tag message for all participants
        sign_up_tags = ' '.join([guild.get_member(user_id).mention for user_id in self.sign_ups if guild.get_member(user_id)])
        await draft_chat_channel_obj.send(f"{sign_up_tags}\nPairings Posted Above")

    def create_pairings_view(self, round_pairings):
        view = discord.ui.View(timeout=None)  # Persistent view
        for player_id, opponent_id, match_number in round_pairings:
            # Determine the style based on whether results have been reported
            match_details = self.match_results.get(match_number, {})
            button_style = discord.ButtonStyle.grey if match_details.get('player1_wins') is not None or match_details.get('player2_wins') is not None else discord.ButtonStyle.primary

            # Instantiate MatchResultButton with the determined style
            button = self.MatchResultButton(self.session_id, match_number, style=button_style)
            view.add_item(button)
        return view

    
    def calculate_pairings(self):
        num_players = len(self.team_a) + len(self.team_b)
        if num_players not in [6, 8]:
            raise ValueError("Unsupported number of players. Only 6 or 8 players are supported.")

        assert len(self.team_a) == len(self.team_b), "Teams must be of equal size."
        
        self.match_results = {}  # Reset or initialize the match results
        pairings = {1: [], 2: [], 3: []}

        # Generate pairings
        for round in range(1, 4):
            round_pairings = []
            for i, player_a in enumerate(self.team_a):
                player_b_index = (i + round - 1) % len(self.team_b)
                player_b = self.team_b[player_b_index]

                match_number = self.match_counter
                self.matches[match_number] = {"players": (player_a, player_b), "results": None}
                self.match_results[match_number] = {"player1_id": player_a, "player1_wins": None, "player2_id": player_b, "player2_wins": None}
                
                round_pairings.append((player_a, player_b, match_number))
                self.match_counter += 1

            pairings[round] = round_pairings

        return pairings

    def create_match(self, player1_id, player2_id):
        match_id = self.match_counter
        self.matches[match_id] = {"players": (player1_id, player2_id), "results": None}
        self.match_counter += 1
        return match_id
    
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

        # Use the generate_draft_summary_embed method to create the embed for the summary
        summary_embed = self.generate_draft_summary_embed()

        # Send the draft summary message to the draft chat channel
        summary_message = await draft_chat_channel.send(embed=summary_embed)
        self.draft_summary_message_id = summary_message.id  # Store the message ID for later updates

        # Delete the original signup message after a delay to clean up
        await asyncio.sleep(10)  # Wait for 10 seconds before deleting the message
        await original_message.delete()

    
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

    
    def generate_draft_summary_embed(self):
        guild = bot.get_guild(self.guild_id)
        if not guild:
            print("Guild not found.")
            return None

        team_a_wins, team_b_wins = self.calculate_team_wins()
        embed = discord.Embed(title=f"Pairings for Draft {self.draft_id} are ready!", color=discord.Color.blue())
        embed.add_field(name="Team A", value="\n".join([guild.get_member(player_id).display_name for player_id in self.team_a]), inline=True)
        embed.add_field(name="Team B", value="\n".join([guild.get_member(player_id).display_name for player_id in self.team_b]), inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=False)  # Spacer
        embed.add_field(name="**Draft Standings**", value=f"**Team A Wins:** {team_a_wins}\n**Team B Wins:** {team_b_wins}", inline=False)

        # Add match results by round
        for round_number, round_pairings in self.pairings.items():
            round_results = f"**Round {round_number} Results**\n"
            for player_a, player_b, match_id in round_pairings:
                player_a_name = guild.get_member(player_a).display_name if guild.get_member(player_a) else "Unknown"
                player_b_name = guild.get_member(player_b).display_name if guild.get_member(player_b) else "Unknown"
                player_a_wins = self.match_results[match_id]['player1_wins'] or 0
                player_b_wins = self.match_results[match_id]['player2_wins'] or 0
                round_results += f"__Match {match_id}__\n{player_a_name}: {player_a_wins} wins\n{player_b_name}: {player_b_wins} wins\n"
            embed.add_field(name=f"Round {round_number}", value=round_results, inline=True)
        
        return embed

    def create_updated_view_for_pairings_message(self, pairings_message_id):
        view = discord.ui.View(timeout=None)
        # Loop through all matches to reconstruct the view
        for match_id, details in self.matches.items():
            if details.get('message_id') == pairings_message_id:
                match_details = self.match_results.get(match_id, {})
                # Determine the button style based on whether results have been reported
                button_style = discord.ButtonStyle.gray if match_details.get('player1_wins') is not None or match_details.get('player2_wins') is not None else discord.ButtonStyle.primary
                # Instantiate a new button with the determined style and the same match number
                button = self.MatchResultButton(self.session_id, match_id, style=button_style)
                view.add_item(button)
        return view

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
        def __init__(self, session_id, match_number, style=discord.ButtonStyle.primary, **kwargs):
            super().__init__(label=f"Match {match_number} Results", style=style, **kwargs)
            self.session_id = session_id
            self.match_number = match_number

        async def callback(self, interaction: discord.Interaction):
            session = sessions.get(self.session_id)
            if session:
                guild = bot.get_guild(session.guild_id)  # Use bot instance to fetch guild
                if guild:
                    view = DraftSession.ResultReportView(self.match_number, session, guild)  # Pass guild to view
                    await interaction.response.send_message(f"Report results for Match {self.match_number}.", view=view, ephemeral=True)
                else:
                    await interaction.response.send_message("Guild not found.", ephemeral=True)

    
    class WinSelect(discord.ui.Select):
        def __init__(self, match_number, player_id, session, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.match_number = match_number
            self.player_id = player_id
            self.session = session

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            match_result = self.session.match_results.get(self.match_number)
            
            if not match_result:
                await interaction.response.send_message("Match result not found.", ephemeral=True)
                return
            
            # Retrieve match result entry
            match_result = self.session.match_results.get(self.match_number)
            if not match_result:
                # Handle case where match result is unexpectedly missing
                await interaction.response.send_message("Match result not found.", ephemeral=True)
                return

            # Determine which player's wins are being updated and update directly
            if self.player_id == match_result['player1_id']:
                match_result['player1_wins'] = int(self.values[0])
            elif self.player_id == match_result['player2_id']:
                match_result['player2_wins'] = int(self.values[0])
            else:
                # Handle unexpected case where player ID doesn't match either player in the match
                await interaction.response.send_message("Player not found in match.", ephemeral=True)
                return

            # Respond to the interaction
            player_name = interaction.guild.get_member(self.player_id).display_name
            await self.session.update_draft_summary()  # Update the draft summary as before
            await self.session.update_pairings_posting(self.match_number)  # Update the pairings message
            await interaction.followup.send(f"Recorded {self.values[0]} wins for {player_name} in Match {self.match_number}.", ephemeral=True)


    class ResultReportView(discord.ui.View):
        def __init__(self, match_number, session, guild):  # Accept guild as a parameter
            super().__init__(timeout=180)
            self.match_number = match_number
            self.session = session
            self.guild = guild  # Store guild object

            player1_id, player2_id = session.matches[self.match_number]["players"]
            player1_name = self.guild.get_member(player1_id).display_name if self.guild.get_member(player1_id) else "Unknown"
            player2_name = self.guild.get_member(player2_id).display_name if self.guild.get_member(player2_id) else "Unknown"

            win_options = [
                discord.SelectOption(label="2 wins", value="2"),
                discord.SelectOption(label="1 win", value="1"),
                discord.SelectOption(label="0 wins", value="0"),
            ]

            self.add_item(self.session.WinSelect(self.match_number, player1_id, self.session, placeholder=f"{player1_name} Wins:", options=win_options, custom_id=f"{self.match_number}_p1"))
            self.add_item(self.session.WinSelect(self.match_number, player2_id, self.session, placeholder=f"{player2_name} Wins:", options=win_options, custom_id=f"{self.match_number}_p2"))

class PersistentView(View):
    def __init__(self, session_id):
        super().__init__(timeout=None)
        self.session_id = session_id

        self.add_item(discord.ui.Button(label="Sign Up", style=discord.ButtonStyle.green, custom_id=f"{session_id}_sign_up"))
        self.add_item(discord.ui.Button(label="Cancel Sign Up", style=discord.ButtonStyle.red, custom_id=f"{session_id}_cancel_sign_up"))
        self.add_item(discord.ui.Button(label="Cancel Draft", style=discord.ButtonStyle.grey, custom_id=f"{session_id}_cancel_draft"))
        self.add_item(discord.ui.Button(label="Remove User", style=discord.ButtonStyle.grey, custom_id=f"{session_id}_remove_user"))
        self.add_item(discord.ui.Button(label="Ready Check", style=discord.ButtonStyle.green, custom_id=f"{session_id}_ready_check"))
        self.add_item(discord.ui.Button(label="Create Teams", style=discord.ButtonStyle.blurple, custom_id=f"{session_id}_randomize_teams"))
        self.add_item(discord.ui.Button(label="Create Chat Rooms", style=discord.ButtonStyle.green, custom_id=f"{session_id}_draft_complete", disabled=True))
        self.add_item(discord.ui.Button(label="Post Pairings", style=discord.ButtonStyle.primary, custom_id=f"{session_id}_post_pairings", disabled=True))

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
        elif interaction.data['custom_id'] == f"{self.session_id}_draft_complete":
            await self.draft_complete_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_post_pairings":
            await self.post_pairings_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_ready_check":
            await self.ready_check_callback(interaction)
        elif interaction.data['custom_id'] == f"{self.session_id}_remove_user":
            await self.remove_user_button_callback(interaction)
            return False
        else:
            return False

        return True

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

        await session.update_draft_complete_message(interaction)
    
    async def ready_check_callback(self, interaction: discord.Interaction):
        session = sessions.get(self.session_id)
        if session:
            await session.initiate_ready_check(interaction)
        else:
            await interaction.response.send_message("Session not found.", ephemeral=True)


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

        if session.team_a is None or session.team_b is None:
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
                        "\n\nAfter the draft, select Create Chat Rooms (give it five seconds to generate rooms) then select Post Pairings" +
                        "\nPost Pairings will take about 10 seconds to process. Only press once.",
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
        self.pairings = session.calculate_pairings()
        await session.move_message_to_draft_channel(bot, original_channel_id, original_message_id, draft_chat_channel_id)

        # Post pairings in the draft chat channel
        await session.post_pairings(interaction.guild, self.pairings)
        
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

            # Optionally, after sending a response, you may want to update or remove the select menu
            # This line will edit the message to only show the text, removing the select menu.
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
    print(f'Logged in as {bot.user}!')
    bot.loop.create_task(cleanup_sessions_task())
               
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

    add_session(session_id, session)

    cube_drafter_role = discord.utils.get(interaction.guild.roles, name="Cube Drafter")
    ping_message = f"{cube_drafter_role.mention if cube_drafter_role else 'Cube Drafter'} Vintage Cube Draft Queue Open!"
    await interaction.followup.send(ping_message, ephemeral=False)

    embed = discord.Embed(
        title=f"Vintage Cube Team Draft Queue - Started <t:{int(draft_start_time)}:R>",
        description="\n**How to use bot**:\n1. Click sign up and click the draftmancer link. Draftmancer host still has to update settings and import the cube.\n" +
                        "2. When enough people join (6 or 8), Push Ready Check. Once everyone is ready, push Create Teams\n" +
                        "3. Create Teams will create randoms teams and a corresponding seating order. Draftmancer host needs to adjust table to match seating order. **TURN OFF RANDOM SEATING IN DRAFTMANCER** \n" +
                        "4. After the draft, come back to this message (it'll be in pins) and click Create Chat Rooms. After 5 seconds chat rooms will be ready and you can press Post Pairings. This takes 10 seconds to process.\n" +
                        "5. You will now have a private team chat with just your team and a shared draft chat that has pairings and match results. You can select the Match Results buttons to report results.\n" +
                        "6. Chat channels will automatically close around five hours after the /startdraft command was used." +
                        f"\n\n**Draftmancer Session**: **[Join Here]({draft_link})**",
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


def add_session(session_id, session):
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
    print(f"Added new session: {session_id}")

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

bot.run(TOKEN)
