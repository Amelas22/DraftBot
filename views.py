import discord
import asyncio
from datetime import datetime
from discord import SelectOption, ButtonStyle
from discord.ui import Button, View, Modal, Select, select
from sqlalchemy import update, select
from session import AsyncSessionLocal, get_draft_session, DraftSession, MatchResult
from sqlalchemy.orm import selectinload
from utils import calculate_pairings, generate_draft_summary_embed ,post_pairings, generate_seating_order, fetch_match_details, update_draft_summary_message, check_and_post_victory_or_draw
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

PROCESSING_ROOMS_PAIRINGS = {}

class PersistentView(discord.ui.View):
    def __init__(self, bot, draft_session_id, session_type, team_a_name=None, team_b_name=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.draft_session_id = draft_session_id
        self.session_type = session_type
        self.team_a_name = team_a_name
        self.team_b_name = team_b_name
        self.channel_ids = []
        self.add_buttons()


    def add_buttons(self):
        if self.session_type == "random":
            self.add_item(self.create_button("Sign Up", "green", f"sign_up_{self.draft_session_id}", self.sign_up_callback))
            self.add_item(self.create_button("Cancel Sign Up", "red", f"cancel_sign_up_{self.draft_session_id}", self.cancel_sign_up_callback))
            self.add_item(self.create_button("Create Teams", "blurple", f"randomize_teams_{self.draft_session_id}", self.randomize_teams_callback))
        elif self.session_type == "premade":
            self.add_item(self.create_button(self.team_a_name, "green", f"Team_A_{self.draft_session_id}", self.team_assignment_callback))
            self.add_item(self.create_button(self.team_b_name, "red", f"Team_B_{self.draft_session_id}", self.team_assignment_callback))
            self.add_item(self.create_button("Generate Seating Order", "blurple", f"generate_seating_{self.draft_session_id}", self.randomize_teams_callback))
        self.add_item(self.create_button("Cancel Draft", "grey", f"cancel_draft_{self.draft_session_id}", self.cancel_draft_callback))
        self.add_item(self.create_button("Remove User", "grey", f"remove_user_{self.draft_session_id}", self.remove_user_button_callback))
        # self.add_item(self.create_button("Ready Check", "green", "ready_check", self.ready_check_callback))
        self.add_item(self.create_button("Create Rooms & Post Pairings", "primary", f"create_rooms_pairings_{self.draft_session_id}", self.create_rooms_pairings_callback, disabled=True))


    def create_button(self, label, style, custom_id, custom_callback, disabled=False):
        style = getattr(discord.ButtonStyle, style)
        button = CallbackButton(label=label, style=style, custom_id=custom_id, custom_callback=custom_callback, disabled=disabled)
        return button



    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        session_exists = await get_draft_session(self.draft_session_id) is not None
        if not session_exists:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
        return session_exists
    
 
    async def sign_up_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Fetch the current draft session to ensure it's up to date
        draft_session = await get_draft_session(self.draft_session_id)
        if not draft_session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return
        
        sign_ups = draft_session.sign_ups or {}

        # Check if the sign-up list is already full
        if len(sign_ups) >= 8:
            await interaction.response.send_message("The sign-up list is already full. No more players can sign up.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        if user_id in sign_ups:
            # User is already signed up; inform them
            await interaction.response.send_message("You are already signed up!", ephemeral=True)
        else:
            # User is signing up
            sign_ups[user_id] = interaction.user.display_name

            # Start an asynchronous database session
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    # Directly update the 'sign_ups' of the draft session
                    await session.execute(
                        update(DraftSession).
                        where(DraftSession.session_id == self.draft_session_id).
                        values(sign_ups=sign_ups)
                    )
                    await session.commit()

            # After committing, re-fetch the draft session to work with updated data
            draft_session_updated = await get_draft_session(self.draft_session_id)
            if not draft_session_updated:
                print("Failed to fetch updated draft session after sign-up.")
                return

            # Confirm signup with draft link
            draft_link = draft_session_updated.draft_link
            signup_confirmation_message = f"You are now signed up. Join Here: {draft_link}"
            await interaction.response.send_message(signup_confirmation_message, ephemeral=True)

            # Update the draft message to reflect the new list of sign-ups
            await update_draft_message(interaction.client, self.draft_session_id)


    async def cancel_sign_up_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        draft_session = await get_draft_session(self.draft_session_id)
        if not draft_session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return
        
        sign_ups = draft_session.sign_ups or {}
        user_id = str(interaction.user.id)
        if user_id not in sign_ups:
            # User is not signed up; inform them
            await interaction.response.send_message("You are not signed up!", ephemeral=True)
        else:
            # User is canceling their sign-up
            del draft_session.sign_ups[user_id]

            # Start an asynchronous database session
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    # Directly update the 'sign_ups' of the draft session
                    await session.execute(
                        update(DraftSession).
                        where(DraftSession.session_id == self.draft_session_id).
                        values(sign_ups=sign_ups)
                    )
                    await session.commit()
            cancel_message = "You're sign up has been canceled!"
            await interaction.response.send_message(cancel_message, ephemeral=True)

            # After committing, re-fetch the draft session to work with updated data
            draft_session_updated = await get_draft_session(self.draft_session_id)
            if not draft_session_updated:
                print("Failed to fetch updated draft session after sign-up.")
                return
            
            # Update the draft message to reflect the new list of sign-ups
            await update_draft_message(interaction.client, self.draft_session_id)
    

    async def randomize_teams_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = interaction.client
        session = await get_draft_session(self.draft_session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return
        session.teams_start_time = datetime.now().timestamp()
        session.session_stage = 'teams'
        # Check session type and prepare teams if necessary
        if session.session_type == 'random':
            from utils import split_into_teams
            await split_into_teams(session.session_id)
            session = await get_draft_session(self.draft_session_id)

        # Generate names for display using the session's sign_ups dictionary
        team_a_display_names = [session.sign_ups[user_id] for user_id in session.team_a]
        team_b_display_names = [session.sign_ups[user_id] for user_id in session.team_b]
        
        seating_order = await generate_seating_order(bot, session)

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
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                # Enable "Create Rooms" and "Cancel Draft" buttons
                if item.custom_id == f"create_rooms_pairings_{self.draft_session_id}" or item.custom_id == f"cancel_draft_{self.draft_session_id}":
                    item.disabled = False
                else:
                    # Disable all other buttons
                    item.disabled = True


        # Respond with the embed and updated view
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def team_assignment_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = await get_draft_session(self.draft_session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return

        user_id = str(interaction.user.id)  # Ensure string format for consistency
        custom_id = button.custom_id
        user_name = interaction.user.display_name

        # Initialize variables to avoid UnboundLocalError
        primary_team = secondary_team = primary_key = secondary_key = None

        # Determine which team the user is trying to interact with
        if "Team_A" in custom_id:
            primary_team, secondary_team = session.team_a or [], session.team_b or []
            primary_key, secondary_key = "team_a", "team_b"
        elif "Team_B" in custom_id:
            primary_team, secondary_team = session.team_b or [], session.team_a or []
            primary_key, secondary_key = "team_b", "team_a"

        # Safety check if the button custom_id doesn't correctly specify a team
        if primary_team is None or secondary_team is None:
            await interaction.response.send_message("An error occurred. Unable to determine the team.", ephemeral=True)
            return

        sign_ups = session.sign_ups or {}

        # Process the team assignment
        if user_id in primary_team:
            # User wants to leave the primary team
            primary_team.remove(user_id)
            action_message = f"You have been removed from {getattr(session, primary_key + '_name', primary_key)}."
        else:
            if user_id in secondary_team:
                # User switches teams
                secondary_team.remove(user_id)
            primary_team.append(user_id)
            action_message = f"You have been added to {getattr(session, primary_key + '_name', primary_key)}."

        # Update or add user in the sign-ups dictionary
        sign_ups[user_id] = user_name

        # Persist changes to the database
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                await db_session.execute(update(DraftSession)
                                        .where(DraftSession.session_id == session.session_id)
                                        .values({primary_key: primary_team, secondary_key: secondary_team, 'sign_ups': sign_ups}))
                await db_session.commit()

        await interaction.response.send_message(action_message, ephemeral=True)

        # Optionally update the message view to reflect the new team compositions
        await self.update_team_view(interaction)
    

    async def cancel_draft_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        session = await get_draft_session(self.draft_session_id)
        if not session:
            await interaction.followup.send("The draft session could not be found.", ephemeral=True)
            return
        user_id = str(interaction.user.id)
        sign_ups = session.sign_ups or {}  # Ensure we have a dictionary

        # Check if the user is in the sign-up list or if the sign-up list is empty
        if sign_ups and user_id not in sign_ups.keys():
            await interaction.followup.send("You do not have permissions to cancel this draft.", ephemeral=True)
            return

        # Delete the draft message if it exists
        channel = self.bot.get_channel(int(session.draft_channel_id))
        if channel:
            try:
                message = await channel.fetch_message(int(session.message_id))
                await message.delete()
            except Exception as e:
                print(f"Failed to delete draft message: {e}")

        # Remove the session from the database
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                await db_session.delete(session)
                await db_session.commit()

        # Send a confirmation message using followup.send
        await interaction.followup.send("The draft has been canceled.", ephemeral=True)


    async def update_team_view(self, interaction: discord.Interaction):
        session = await get_draft_session(self.draft_session_id)
        if not session:
            print("Draft session not found.")
            return

        channel = self.bot.get_channel(int(session.draft_channel_id))
        if channel is None:
            print(f"Channel not found for draft session ID {self.draft_session_id}.")
            return

        message = await channel.fetch_message(int(session.message_id))
        embed = message.embeds[0]  # Assuming there's only one embed attached to the message

        # Assume team_a_names and team_b_names are prepared earlier in the method
        team_a_names = [session.sign_ups.get(str(user_id), "Unknown User") for user_id in (session.team_a or [])]
        team_b_names = [session.sign_ups.get(str(user_id), "Unknown User") for user_id in (session.team_b or [])]

        # Find the index of the Team A and Team B fields in the embed
        team_a_index = next((i for i, e in enumerate(embed.fields) if e.name.startswith(session.team_a_name or "Team A")), None)
        team_b_index = next((i for i, e in enumerate(embed.fields) if e.name.startswith(session.team_b_name or "Team B")), None)

        # Update the fields if found
        if team_a_index is not None:
            embed.set_field_at(team_a_index, name=f"{session.team_a_name} ({len(session.team_a or [])}):", value="\n".join(team_a_names) if team_a_names else "No players yet.", inline=True)
        if team_b_index is not None:
            embed.set_field_at(team_b_index, name=f"{session.team_b_name} ({len(session.team_b or [])}):", value="\n".join(team_b_names) if team_b_names else "No players yet.", inline=True)

        # Edit the original message with the updated embed
        await message.edit(embed=embed)
    

    async def remove_user_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = await get_draft_session(self.draft_session_id)
        if not session:
            print("Draft session not found.")
            return

        # Check if the user initiating the remove action is in the sign_ups
        if str(interaction.user.id) not in session.sign_ups:
            await interaction.response.send_message("You are not authorized to remove users.", ephemeral=True)
            return

        # If the session exists and has sign-ups, and the user is authorized, proceed
        if session.sign_ups:
            options = [discord.SelectOption(label=user_name, value=user_id) for user_id, user_name in session.sign_ups.items()]
            view = UserRemovalView(session_id=session.session_id, options=options)
            await interaction.response.send_message("Select a user to remove:", view=view, ephemeral=True)
        else:
            await interaction.response.send_message("No users to remove.", ephemeral=True)
        
    async def create_rooms_pairings_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        session_id = self.draft_session_id
        if PROCESSING_ROOMS_PAIRINGS.get(session_id):
            # Immediately inform the user that the process is already underway
            await interaction.response.send_message("The rooms and pairings are currently being created. Please wait.", ephemeral=True)
            return
        else:
            # Mark the session as being processed
            PROCESSING_ROOMS_PAIRINGS[session_id] = True
        
        await interaction.response.defer()
        

        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                stmt = select(DraftSession).options(selectinload(DraftSession.match_results)).filter(DraftSession.session_id == self.draft_session_id)
                session = await db_session.scalar(stmt)

                if not session:
                    print("Draft session not found.")
                    await interaction.followup.send("Draft session not found.", ephemeral=True)
                    return

                if session.are_rooms_processing:
                    await interaction.followup.send("The rooms and pairings are currently being created. Please wait.", ephemeral=True)
                    return

                session.are_rooms_processing = True
                session.session_stage = 'pairings'

                await calculate_pairings(session, db_session)

                guild = interaction.guild
                bot = interaction.client
                # Immediately disable the "Create Rooms & Post Pairings" button to prevent multiple presses
                for child in self.children:
                    if isinstance(child, discord.ui.Button) and child.label == "Create Rooms & Post Pairings":
                        child.disabled = True
                        break

                # Execute tasks to create chat channels
                team_a_members = [guild.get_member(int(user_id)) for user_id in session.team_a if guild.get_member(int(user_id))]
                team_b_members = [guild.get_member(int(user_id)) for user_id in session.team_b if guild.get_member(int(user_id))]
                all_members = team_a_members + team_b_members

                session.draft_chat_channel = str(await self.create_team_channel(guild, "Draft", all_members, session.team_a, session.team_b))
                await self.create_team_channel(guild, "Team-A", team_a_members, session.team_a, session.team_b)
                await self.create_team_channel(guild, "Team-B", team_b_members, session.team_a, session.team_b)

                # Fetch the channel object using the ID
                draft_chat_channel = guild.get_channel(int(session.draft_chat_channel))
                draft_summary_embed = await generate_draft_summary_embed(bot, session.session_id)

                if draft_chat_channel and draft_summary_embed:
                    sign_up_tags = ' '.join([f"<@{user_id}>" for user_id in session.sign_ups.keys()])
                    await draft_chat_channel.send(f"Pairing posted below. Good luck in your matches! {sign_up_tags}")
                    draft_summary_message = await draft_chat_channel.send(embed=draft_summary_embed)
                    session.draft_summary_message_id = str(draft_summary_message.id)
                
                draft_channel_id = int(session.draft_channel_id)  # Ensure this is where the message exists
                original_message_id = int(session.message_id)

                # Fetch the channel and delete the message
                draft_channel = interaction.client.get_channel(draft_channel_id)
                if draft_channel:
                    try:
                        original_message = await draft_channel.fetch_message(original_message_id)
                        await original_message.delete()
                    except discord.NotFound:
                        print(f"Original message {original_message_id} not found in channel {draft_channel_id}.")
                    except discord.HTTPException as e:
                        print(f"Failed to delete message {original_message_id}: {e}")


                await db_session.commit()
            # Execute Post Pairings
            await post_pairings(bot, guild, session.session_id)
            del PROCESSING_ROOMS_PAIRINGS[session_id]
            await interaction.followup.send("Chat rooms created and pairings posted.", ephemeral=True)

    async def create_team_channel(self, guild, team_name, team_members, team_a, team_b):
        draft_category = discord.utils.get(guild.categories, name="Draft Channels")
        session = await get_draft_session(self.draft_session_id)
        if not session:
            print("Draft session not found.")
            return
        channel_name = f"{team_name}-Chat-{session.draft_id}"

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

        if team_name == "Draft":
            self.draft_chat_channel = channel.id

        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                update_values = {
                    'channel_ids': self.channel_ids,
                    'draft_chat_channel': self.draft_chat_channel,
                    'session_stage': 'pairings'
                }
                await db_session.execute(update(DraftSession)
                                        .where(DraftSession.session_id == self.draft_session_id)
                                        .values(**update_values))
                await db_session.commit()

        return channel.id

class UserRemovalSelect(Select):
    def __init__(self, options: list[SelectOption], session_id: str, *args, **kwargs):
        super().__init__(*args, **kwargs, placeholder="Choose a user to remove...", min_values=1, max_values=1, options=options)
        self.session_id = session_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        bot = interaction.client
        session = await get_draft_session(self.session_id)

        user_id_to_remove = self.values[0]  
        if user_id_to_remove in session.sign_ups:
            removed_user_name = session.sign_ups.pop(user_id_to_remove)
            
            async with AsyncSessionLocal() as db_session:
                async with db_session.begin():
                    # Update the session in the database
                    await db_session.execute(update(DraftSession)
                                            .where(DraftSession.session_id == session.session_id)
                                            .values(sign_ups=session.sign_ups))
                    await db_session.commit()
            # After removing a user, update the original message with the new sign-up list
            if session.session_type == "random":
                await update_draft_message(bot, session_id=session.session_id)
            else:
                await self.update_team_view(interaction)

            await interaction.followup.send(f"Removed {removed_user_name} from the draft.")
        else:
            await interaction.response.send_message("User not found in sign-ups.", ephemeral=True)


async def create_pairings_view(bot, guild, session_id, match_results):
    view = View(timeout=None)
    for match_result in match_results:
        # Fetch player names for the button labels
        player1 = guild.get_member(int(match_result.player1_id))
        player2 = guild.get_member(int(match_result.player2_id))
        player1_name = player1.display_name if player1 else 'Unknown Player'
        player2_name = player2.display_name if player2 else 'Unknown Player'
        
        # Initialize a MatchResultButton for each match
        button = MatchResultButton(
            bot=bot,
            session_id=session_id,
            match_id=match_result.id,  # Assuming match_result has an id attribute
            match_number=match_result.match_number,
            label=f"Match {match_result.match_number} Results",
            style=discord.ButtonStyle.primary,
            row=None  # Optionally, specify a row for button placement
        )
        view.add_item(button)
    return view


class MatchResultButton(Button):
    def __init__(self, bot, session_id, match_id, match_number, label, *args, **kwargs):
        super().__init__(label=label, *args, **kwargs)
        self.bot = bot
        self.session_id = session_id
        self.match_id = match_id
        self.match_number = match_number

    async def callback(self, interaction):
        await interaction.response.defer()

        # Fetch player names and IDs
        player1_name, player2_name = await fetch_match_details(self.bot, self.session_id, self.match_number)
        
        # Create a Select menu for reporting the result
        match_result_select = MatchResultSelect(
            match_number=self.match_number,
            bot = self.bot,
            session_id=self.session_id, 
            player1_name=player1_name, 
            player2_name=player2_name
        )

        # Create and send a new View containing the Select menu
        view = View()
        view.add_item(match_result_select)
        await interaction.followup.send("Please select the match result:", view=view, ephemeral=True)


class MatchResultSelect(Select):
    def __init__(self, bot, match_number, session_id, player1_name, player2_name, *args, **kwargs):
        self.bot = bot
        self.match_number = match_number
        self.session_id = session_id

        options = [
            SelectOption(label=f"{player1_name} wins: 2-0", value="2-0-1"),
            SelectOption(label=f"{player1_name} wins: 2-1", value="2-1-1"),
            SelectOption(label=f"{player2_name} wins: 2-0", value="0-2-2"),
            SelectOption(label=f"{player2_name} wins: 2-1", value="1-2-2"),
            SelectOption(label="No Match Played", value="0-0-0"),
        ]
        super().__init__(placeholder=f"{player1_name} v. {player2_name}", min_values=1, max_values=1, options=options, *args, **kwargs)

    async def callback(self, interaction):
        # Splitting the selected value to get the result details
        await interaction.response.defer()
        player1_wins, player2_wins, winner_indicator = self.values[0].split('-')
        player1_wins = int(player1_wins)
        player2_wins = int(player2_wins)
        winner_id = None  # Default to None in case of a draw

        async with AsyncSessionLocal() as session:  # Use your session creation method here
            async with session.begin():
                # Fetch the match result entry from the database
                stmt = select(MatchResult).where(
                    MatchResult.session_id == self.session_id,
                    MatchResult.match_number == self.match_number
                )
                result = await session.execute(stmt)
                match_result = result.scalars().first()
                if match_result:
                    # Update the match result based on the selection
                    match_result.player1_wins = player1_wins
                    match_result.player2_wins = player2_wins
                    if winner_indicator != '0':  # Determine the winner_id if there's a clear winner
                        winner_id = match_result.player1_id if winner_indicator == '1' else match_result.player2_id
                    match_result.winner_id = winner_id

                    await session.commit()  # Commit the changes to the database

                   
        await update_draft_summary_message(self.bot, self.session_id)
        await check_and_post_victory_or_draw(self.bot, self.session_id)
        await self.update_pairings_posting(interaction, self.bot, self.session_id, self.match_number) 
                    
    async def update_pairings_posting(self, interaction, bot, draft_session_id, match_number):
        guild = bot.get_guild(int(interaction.guild_id))

        if not guild:
            print("Guild not found.")
            return

        async with AsyncSessionLocal() as session:
            # Fetch the MatchResult to get the pairing_message_id
            stmt = select(MatchResult).where(
                MatchResult.session_id == draft_session_id,
                MatchResult.match_number == match_number
            )
            result = await session.execute(stmt)
            match_result = result.scalar_one_or_none()

            if not match_result:
                print(f"No match result found for match number {match_number} in session {draft_session_id}.")
                return

            pairing_message_id = match_result.pairing_message_id
            if not pairing_message_id:
                print(f"No pairing message ID found for match number {match_number}.")
                return

            # Fetch the DraftSession to get the channel ID
            draft_session_result = await session.execute(select(DraftSession).filter_by(session_id=draft_session_id))
            draft_session = draft_session_result.scalar_one_or_none()

            if not draft_session:
                print("Draft session not found.")
                return

            channel = guild.get_channel(int(draft_session.draft_chat_channel))
            if not channel:
                print("Channel not found.")
                return

            # Fetch the specific message using the pairing_message_id
            try:
                message = await channel.fetch_message(int(pairing_message_id))
            except Exception as e:
                print(f"Failed to fetch message with ID {pairing_message_id}: {e}")
                return

            # Now you can proceed with updating the message as previously outlined
            embed = message.embeds[0] if message.embeds else None
            if not embed:
                print("No embed found in the pairings message.")
                return

            # Fetch MatchResults for the specific draft session
            stmt = select(MatchResult).where(MatchResult.session_id == draft_session_id, MatchResult.pairing_message_id == pairing_message_id)
            result = await session.execute(stmt)
            match_results_for_this_message = result.scalars().all()

            # Update the embed with new match results
            for match_result in match_results_for_this_message:
                if match_result.match_number == match_number:
                    player1, player2 = guild.get_member(int(match_result.player1_id)), guild.get_member(int(match_result.player2_id))
                    player1_name, player2_name = player1.display_name if player1 else 'Unknown', player2.display_name if player2 else 'Unknown'
                    updated_value = f"**Match {match_result.match_number}**\n{player1_name}: {match_result.player1_wins} wins\n{player2_name}: {match_result.player2_wins} wins"
                    
                    for i, field in enumerate(embed.fields):
                        if f"**Match {match_result.match_number}**" in field.value:
                            embed.set_field_at(i, name=field.name, value=updated_value, inline=field.inline)
                            break

            new_view = await self.create_updated_view_for_pairings_message(bot, guild.id, draft_session_id, pairing_message_id)

            # Edit the message with the updated embed and view
            await message.edit(embed=embed, view=new_view)

    async def create_updated_view_for_pairings_message(self, bot, guild_id, draft_session_id, pairing_message_id):
        guild = bot.get_guild(guild_id)
        if not guild:
            print("Guild not found.")
            return discord.ui.View()  # Return an empty view if the guild is not found.

        view = discord.ui.View(timeout=None)
        async with AsyncSessionLocal() as session:
            # Fetch MatchResults associated with this specific pairing_message_id
            stmt = select(MatchResult).where(
                MatchResult.session_id == draft_session_id,
                MatchResult.pairing_message_id == pairing_message_id
            )
            result = await session.execute(stmt)
            match_results = result.scalars().all()

            for match_result in match_results:
                # Check if a winner has been reported for this match.
                has_winner_reported = match_result.winner_id is not None

                # Determine the button style: grey if a winner has been reported, otherwise primary.
                button_style = discord.ButtonStyle.grey if has_winner_reported else discord.ButtonStyle.primary

                # Create a button for each match. Assume MatchResultButton class exists and works as intended.
                button = MatchResultButton(
                    bot=bot,
                    session_id=draft_session_id,
                    match_id=match_result.id,  # Ensure this correctly targets the unique identifier for the MatchResult.
                    match_number=match_result.match_number,
                    label=f"Match {match_result.match_number} Results",
                    style=button_style
                )

                # Add the newly created button to the view.
                view.add_item(button)

        return view

class UserRemovalView(discord.ui.View):
    def __init__(self, session_id: str, options: list[discord.SelectOption]):
        super().__init__()
        self.add_item(UserRemovalSelect(options=options, session_id=session_id))


class CallbackButton(discord.ui.Button):
    def __init__(self, *, label, style, custom_id, custom_callback, disabled=False):
        super().__init__(label=label, style=style, custom_id=custom_id, disabled=disabled)
        self.custom_callback = custom_callback  

    async def callback(self, interaction: discord.Interaction):
        await self.custom_callback(interaction, self)


async def update_draft_message(bot, session_id):
    draft_session = await get_draft_session(session_id)
    if not draft_session:
        print("Failed to fetch draft session for updating the message.")
        return

    channel_id = int(draft_session.draft_channel_id)
    message_id = int(draft_session.message_id)
    channel = bot.get_channel(channel_id)

    if not channel:
        print(f"Channel with ID {channel_id} not found.")
        return

    try:
        message = await channel.fetch_message(message_id)
        embed = message.embeds[0]  # Assuming there's at least one embed in the message
        sign_up_count = len(draft_session.sign_ups)
        sign_ups_field_name = f"Sign-Ups ({sign_up_count}):"
        sign_ups_str = '\n'.join([f"{name}" for name in draft_session.sign_ups.values()]) if draft_session.sign_ups else 'No players yet.'
        embed.set_field_at(0, name=sign_ups_field_name, value=sign_ups_str, inline=False)
        await message.edit(embed=embed)
    except Exception as e:
        print(f"Failed to update message for session {session_id}. Error: {e}")

# class PersistentView(discord.ui.View):
#     def __init__(self, draft_session):
#         super().__init__(timeout=None)
#         self.draft_session = draft_session
        
#         if self.draft_session.session_type == 'premade':
#             self.add_item(discord.ui.Button(label=f"{self.draft_session.team_a_name}", style=discord.ButtonStyle.green, custom_id=f"{self.draft_session.session_id}_Team_A"))
#             self.add_item(discord.ui.Button(label=f"{self.draft_session.team_b_name}", style=discord.ButtonStyle.red, custom_id=f"{self.draft_session.session_id}_Team_B"))
#             self.add_item(discord.ui.Button(label="Generate Seating Order", style=discord.ButtonStyle.blurple, custom_id=f"{self.draft_session.session_id}_generate_seating"))
#         elif self.draft_session.session_type == 'random':
#             self.add_item(discord.ui.Button(label="Sign Up", style=discord.ButtonStyle.green, custom_id=f"{self.draft_session.session_id}_sign_up"))
#             self.add_item(discord.ui.Button(label="Cancel Sign Up", style=discord.ButtonStyle.red, custom_id=f"{self.draft_session.session_id}_cancel_sign_up"))
#             self.add_item(discord.ui.Button(label="Create Teams", style=discord.ButtonStyle.blurple, custom_id=f"{self.draft_session.session_id}_randomize_teams"))
                
#         self.add_item(discord.ui.Button(label="Cancel Draft", style=discord.ButtonStyle.grey, custom_id=f"{self.draft_session.session_id}_cancel_draft"))
#         self.add_item(discord.ui.Button(label="Remove User", style=discord.ButtonStyle.grey, custom_id=f"{self.draft_session.session_id}_remove_user"))
#         self.add_item(discord.ui.Button(label="Ready Check", style=discord.ButtonStyle.green, custom_id=f"{self.draft_session.session_id}_ready_check"))
#         self.add_item(discord.ui.Button(label="Create Rooms & Post Pairings", style=discord.ButtonStyle.primary, custom_id=f"{self.draft_session.session_id}_create_rooms_pairings", disabled=True))

#     async def interaction_check(self, interaction: discord.Interaction) -> bool:
#         custom_id = interaction.data['custom_id']
#             # If none of the conditions match, the interaction is not recognized and you might want to log this case.
#             return False

        
    
    # async def ready_check_callback(self, interaction: discord.Interaction):
    #     session = sessions.get(self.session_id)
    #     if session:
    #         # Check if the user is in the sign-up list
    #         if interaction.user.id in session.sign_ups:
    #             # Proceed with the ready check
    #             await session.initiate_ready_check(interaction)

    #             # Disable the "Ready Check" button after use
    #             for item in self.children:
    #                 if isinstance(item, discord.ui.Button) and item.custom_id == f"{self.session_id}_ready_check":
    #                     item.disabled = True
    #                     break  # Stop the loop once the button is found and modified

    #             # Ensure the view reflects the updated state with the button disabled
    #             await interaction.edit_original_response(view=self)
    #         else:
    #             # Inform the user they're not in the sign-up list, hence can't initiate a ready check
    #             await interaction.response.send_message("You must be signed up to initiate a ready check.", ephemeral=True)
    #     else:
    #         await interaction.response.send_message("Session not found.", ephemeral=True)


    # async def team_assignment_callback(self, interaction: discord.Interaction):
    #     session = sessions.get(self.session_id)
    #     if not session:
    #         await interaction.response.send_message("Session not found.", ephemeral=True)
    #         return

    #     user_id = interaction.user.id
    #     custom_id = interaction.data["custom_id"]
    #     user_name = interaction.user.display_name

    #     if "_Team_A" in custom_id:
    #         primary_team_key = "team_a"
    #         secondary_team_key = "team_b"
    #     elif "_Team_B" in custom_id:
    #         primary_team_key = "team_b"
    #         secondary_team_key = "team_a"
    #     else:
    #         await interaction.response.send_message("An error occurred.", ephemeral=True)
    #         return

    #     primary_team = getattr(session, primary_team_key, [])
    #     secondary_team = getattr(session, secondary_team_key, [])

    #     # Add or remove the user from the team lists
    #     if user_id in primary_team:
    #         primary_team.remove(user_id)
    #         del session.sign_ups[user_id]  # Remove from sign-ups dictionary
    #         action_message = f"You have been removed from a team."
    #     else:
    #         if user_id in secondary_team:
    #             secondary_team.remove(user_id)
    #             del session.sign_ups[user_id]  # Remove from sign-ups dictionary before re-adding to correct team
    #         primary_team.append(user_id)
    #         session.sign_ups[user_id] = user_name  # Add/update in sign-ups dictionary
    #         action_message = f"You have been added to a team."

    #     # Update session attribute to reflect changes
    #     setattr(session, primary_team_key, primary_team)
    #     setattr(session, secondary_team_key, secondary_team)

    #     await interaction.response.send_message(action_message, ephemeral=True)
    #     await session.update_team_view(interaction)

    
            

