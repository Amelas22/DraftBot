import discord
import asyncio
import random
import pytz
from datetime import datetime, timedelta
from discord import SelectOption
from discord.ui import Button, View, Select, select
from session import StakeInfo, AsyncSessionLocal, get_draft_session, DraftSession, MatchResult
from stake_calculator import StakeCalculator, StakePair
from sqlalchemy import update, select, and_
from sqlalchemy.orm import selectinload
from utils import calculate_pairings, create_winston_draft, generate_draft_summary_embed ,post_pairings, generate_seating_order, fetch_match_details, update_draft_summary_message, check_and_post_victory_or_draw, update_player_stats_and_elo, check_weekly_limits, update_player_stats_for_draft
from loguru import logger

PROCESSING_ROOMS_PAIRINGS = {}
sessions = {}

class PersistentView(discord.ui.View):
    def __init__(self, bot, draft_session_id, session_type=None, team_a_name=None, team_b_name=None, session_stage=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.draft_session_id = draft_session_id
        self.session_type = session_type
        self.team_a_name = team_a_name
        self.team_b_name = team_b_name
        self.session_stage = session_stage
        self.channel_ids = []
        self.add_buttons()

    def to_metadata(self) -> dict:
        """Convert view properties to a dictionary for JSON storage."""
        return {
            "draft_session_id": self.draft_session_id,
            "session_type": self.session_type,
            "team_a_name": self.team_a_name,
            "team_b_name": self.team_b_name,
            "session_stage": self.session_stage,  
        }

    @classmethod
    def from_metadata(cls, bot, metadata: dict):
        """Recreate a PersistentView from stored metadata."""
        return cls(
            bot=bot,
            draft_session_id=metadata.get("draft_session_id"),
            session_type=metadata.get("session_type"),
            team_a_name=metadata.get("team_a_name"),
            team_b_name=metadata.get("team_b_name"),
            session_stage=metadata.get("session_stage"),
        )

    def add_buttons(self):
        if self.session_type == "winston":
            self.add_item(self.create_button("Sign Up", "green", f"sign_up_{self.draft_session_id}", self.sign_up_callback))
            self.add_item(self.create_button("Cancel Sign Up", "red", f"cancel_sign_up_{self.draft_session_id}", self.cancel_sign_up_callback))
            self.add_item(self.create_button("Cancel Draft", "grey", f"cancel_draft_{self.draft_session_id}", self.cancel_draft_callback))
            self.add_item(self.create_button("Remove User", "grey", f"remove_user_{self.draft_session_id}", self.remove_user_button_callback))
        else:
            if self.session_type != "premade":
                self.add_item(self.create_button("Sign Up", "green", f"sign_up_{self.draft_session_id}", self.sign_up_callback))
                self.add_item(self.create_button("Cancel Sign Up", "red", f"cancel_sign_up_{self.draft_session_id}", self.cancel_sign_up_callback))
                if self.session_type == "swiss":
                    self.add_item(self.create_button("Generate Seating Order", "blurple", f"randomize_teams_{self.draft_session_id}", self.randomize_teams_callback))
                elif self.session_type == "test" or self.session_type == "schedule":
                    self.add_item(self.create_button("Cancel Draft", "grey", f"cancel_draft_{self.draft_session_id}", self.cancel_draft_callback))
                    self.add_item(self.create_button("Remove User", "grey", f"remove_user_{self.draft_session_id}", self.remove_user_button_callback))
                    return
                else:
                    self.add_item(self.create_button("Create Teams", "blurple", f"randomize_teams_{self.draft_session_id}", self.randomize_teams_callback))
            elif self.session_type == "premade":
                self.add_item(self.create_button(self.team_a_name, "green", f"Team_A_{self.draft_session_id}", self.team_assignment_callback))
                self.add_item(self.create_button(self.team_b_name, "red", f"Team_B_{self.draft_session_id}", self.team_assignment_callback))
                # draft_button_label = "League Draft: ON"
                # draft_button_style = "green"
                # self.add_item(self.create_button(draft_button_label, draft_button_style, f"track_draft_{self.draft_session_id}", self.track_draft_callback))
                self.add_item(self.create_button("Generate Seating Order", "primary", f"generate_seating_{self.draft_session_id}", self.randomize_teams_callback))
            self.add_item(self.create_button("Cancel Draft", "grey", f"cancel_draft_{self.draft_session_id}", self.cancel_draft_callback))
            self.add_item(self.create_button("Remove User", "grey", f"remove_user_{self.draft_session_id}", self.remove_user_button_callback))
            
            if self.session_type != "test":
            #    self.add_item(self.create_button("Post Pairings", "primary", f"create_rooms_pairings_{self.draft_session_id}", self.create_rooms_pairings_callback, disabled=True))
            #else:
                self.add_item(self.create_button("Ready Check", "green", f"ready_check_{self.draft_session_id}", self.ready_check_callback))
                self.add_item(self.create_button("Create Rooms & Post Pairings", "primary", f"create_rooms_pairings_{self.draft_session_id}", self.create_rooms_pairings_callback, disabled=True))

            # Logic to enable/disable based on session_stage
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    if self.session_stage == "teams":
                        if item.custom_id == f"create_rooms_pairings_{self.draft_session_id}" or item.custom_id == f"cancel_draft_{self.draft_session_id}":
                            item.disabled = False
                        else:
                            item.disabled = True
    def create_button(self, label, style, custom_id, custom_callback, disabled=False):
        style = getattr(discord.ButtonStyle, style)
        button = CallbackButton(label=label, style=style, custom_id=custom_id, custom_callback=custom_callback, disabled=disabled)
        return button



    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        session_exists = await get_draft_session(self.draft_session_id) is not None
        if not session_exists:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
        return session_exists
    
    async def track_draft_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with AsyncSessionLocal() as session:
            async with session.begin():
                draft_session = await get_draft_session(self.draft_session_id)
                # Directly update the 'sign_ups' of the draft session
                await session.execute(
                    update(DraftSession).
                    where(DraftSession.session_id == self.draft_session_id).
                    values(tracked_draft = not draft_session.tracked_draft)
                )
                await session.commit()
        
        draft_session = await get_draft_session(self.draft_session_id)
        # update the button's label and style directly based on the new tracked_draft state
        # Find the specific button to update
        track_draft_button = next((btn for btn in self.children if btn.custom_id == f"track_draft_{self.draft_session_id}"), None)
        if track_draft_button:
            track_draft_button.label = "League Draft: ON" if draft_session.tracked_draft else "League Draft: OFF"
            track_draft_button.style = discord.ButtonStyle.green if draft_session.tracked_draft else discord.ButtonStyle.red
            await interaction.response.edit_message(view=self)  # Reflect these changes in the message

            # Optionally, confirm the update to the user
            await interaction.followup.send(f"League draft status updated: {'ON' if draft_session.tracked_draft else 'OFF'}", ephemeral=True)


    async def sign_up_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.session_type == "winston":
            now = datetime.now()
            deletion_time = now + timedelta(minutes=10)
            relative_time = f"<t:{int(deletion_time.timestamp())}:R>"

        # Fetch the current draft session to ensure it's up to date
        draft_session = await get_draft_session(self.draft_session_id)
        if not draft_session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return
        
        sign_ups = draft_session.sign_ups or {}

        # Check if the sign-up list is already full
        if self.session_type != "schedule":
            if len(sign_ups) >= 8:
                await interaction.response.send_message("The sign-up list is already full. No more players can sign up.", ephemeral=True)
                return
        user_id = str(interaction.user.id)

        if draft_session.session_type == "swiss":
            pacific = pytz.timezone('US/Pacific')
            utc = pytz.utc
            now = datetime.now()
            pacific_time = utc.localize(now).astimezone(pacific)
            midnight_pacific = pacific.localize(datetime(pacific_time.year, pacific_time.month, pacific_time.day))
            start_of_week = midnight_pacific - timedelta(days=midnight_pacific.weekday())
            async with AsyncSessionLocal() as db_session:
                async with db_session.begin():
                    from session import PlayerLimit
                    player_weekly_limit_stmt = select(PlayerLimit).where(
                                PlayerLimit.player_id == user_id,
                                PlayerLimit.WeekStartDate == start_of_week
                        )
                    player_weekly_limit_result = await db_session.execute(player_weekly_limit_stmt)
                    player_weekly_limit = player_weekly_limit_result.scalars().first()
                    
                    if player_weekly_limit:
                        if player_weekly_limit.drafts_participated >= 4:
                            await interaction.response.send_message("You have already participated in four drafts this week! Next week begins Monday at midnight pacific time. If you believe this is an error, please contact a Cube Overseer", ephemeral=True)
                            return

        if user_id in sign_ups:
            # User is already signed up; inform them
            await interaction.response.send_message("You are already signed up!", ephemeral=True)
        else:            
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

            # Handle staked drafts differently - show modal first
            if self.session_type == "staked":
                # Respond with the modal - this has to be the first response
                stake_modal = StakeModal(self.draft_session_id, draft_link=draft_session_updated.draft_link)
                await interaction.response.send_modal(stake_modal)
                
                # The modal callback will handle updating the draft message
                return
                
            # For non-staked drafts, continue with the normal flow
            # Confirm signup with draft link
            draft_link = draft_session_updated.draft_link
            signup_confirmation_message = f"You are now signed up. Join Here: {draft_link}"
            await interaction.response.send_message(signup_confirmation_message, ephemeral=True)

            # Update the draft message to reflect the new list of sign-ups
            await update_draft_message(interaction.client, self.draft_session_id)
            
            if self.session_type == "winston":
                if len(sign_ups) == 2:
                    sign_up_tags = ' '.join([f"<@{user_id}>" for user_id in draft_session_updated.sign_ups.keys()])
                    guild = self.bot.get_guild(int(interaction.guild_id))
                    channel = discord.utils.get(guild.text_channels, name="winston-draft")
                    await channel.send(f"Winston Draft Ready. Good luck in your match! {sign_up_tags}")
                    await create_winston_draft(self.bot, interaction)
                else:
                    guild = interaction.guild
                    message_link = f"https://discord.com/channels/{draft_session_updated.guild_id}/{draft_session_updated.draft_channel_id}/{draft_session_updated.message_id}"
                    channel = discord.utils.get(guild.text_channels, name="cube-draft-open-play")
                    await channel.send(f"**{interaction.user.display_name}** is looking for an opponent for a **Winston Draft**. [Join Here!]({message_link}) ")

                    
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
            cancel_message = "Your sign up has been canceled!"
            await interaction.response.send_message(cancel_message, ephemeral=True)

            # After committing, re-fetch the draft session to work with updated data
            draft_session_updated = await get_draft_session(self.draft_session_id)
            if not draft_session_updated:
                print("Failed to fetch updated draft session after sign-up.")
                return
            
            # Update the draft message to reflect the new list of sign-ups
            await update_draft_message(interaction.client, self.draft_session_id)
    
    async def ready_check_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Fetch the session data from the database
        session = await get_draft_session(self.draft_session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        if user_id not in session.sign_ups:
            await interaction.response.send_message("You are not registered in the draft session.", ephemeral=True)
            return
        
        # Create a dictionary to store the initial ready check status
        ready_check_status = {
            "ready": [],
            "not_ready": [],
            "no_response": list(session.sign_ups.keys())
        }

        # Save this status in a global sessions dictionary
        sessions[self.draft_session_id] = ready_check_status

        # Disable the "Ready Check" button
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id.endswith("ready_check"):
                item.disabled = True
                break

        # Generate the initial embed
        embed = await generate_ready_check_embed(ready_check_status=ready_check_status, sign_ups=session.sign_ups)
        
        # Create the view with the buttons
        view = ReadyCheckView(self.draft_session_id)

        # Send the initial ready check message
        main_message = await interaction.response.send_message(embed=embed, view=view, ephemeral=False)

        # Construct a message that mentions all users who need to respond to the ready check
        user_mentions = ' '.join([f"<@{user_id}>" for user_id in session.sign_ups.keys()])
        mention_message = f"Ready Check Initiated {user_mentions}"

        # Send the mention message as a follow-up to ensure it gets sent after the embed
        await interaction.followup.send(mention_message, ephemeral=False)

        asyncio.create_task(self.cleanup_ready_check(self.draft_session_id))


    async def cleanup_ready_check(self, draft_session_id):
        await asyncio.sleep(600)  # Wait for 10 minutes
        try:
            if draft_session_id in sessions:
                del sessions[draft_session_id]  # Clean up the session data
        except discord.NotFound:
            # The message was already deleted or not found
            pass
        except Exception as e:
            print(f"Failed to delete ready check message: {e}")

    async def randomize_teams_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = interaction.client
        session_id = self.draft_session_id
        
        # Check for stake-related requirements first
        if self.session_type == "staked":
            # Check that all players have set their stakes
            from utils import get_missing_stake_players
            missing_players = await get_missing_stake_players(session_id)
            if missing_players:
                # Get display names for the missing players
                guild = bot.get_guild(int(interaction.guild_id))
                missing_names = []
                for pid in missing_players:
                    member = guild.get_member(int(pid))
                    if member:
                        missing_names.append(member.display_name)
                
                # Format error message
                players_str = ", ".join(missing_names)
                await interaction.response.send_message(
                    f"Cannot create teams yet. The following players need to set their stakes: {players_str}",
                    ephemeral=True
                )
                return
            
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                stmt = select(DraftSession).where(DraftSession.session_id == session_id)
                result = await db_session.execute(stmt)
                session = result.scalars().first()

                if not session:
                    await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
                    return
                    
                if len(session.sign_ups) % 2 != 0:
                    await interaction.response.send_message("There must be an even number of players to fire.")
                    return

                # Update the session object
                session.teams_start_time = datetime.now()
                if session.session_type == 'premade':
                    # 7 days for premade drafts (league matches)
                    session.deletion_time = datetime.now() + timedelta(days=7)
                else:
                    # 4 hours for other draft types
                    session.deletion_time = datetime.now() + timedelta(hours=4)
                session.session_stage = 'teams'
                
                # Check session type and prepare teams if necessary
                if session.session_type == 'random' or session.session_type == 'test' or session.session_type == 'staked':
                    from utils import split_into_teams
                    await split_into_teams(bot, session.session_id)
                    # Re-fetch session to get updated teams
                    updated_session = await get_draft_session(self.draft_session_id)
                    
                    # Now that teams exist, calculate stakes for staked drafts
                    stake_pairs = []
                    stake_info_by_player = {}
                    
                    if self.session_type == "staked" and updated_session and updated_session.team_a and updated_session.team_b:
                        # Calculate and store stakes in database
                        await self.calculate_and_store_stakes(interaction, updated_session)
                        
                        # Fetch the calculated stakes for display
                        stake_stmt = select(StakeInfo).where(StakeInfo.session_id == session_id)
                        stake_results = await db_session.execute(stake_stmt)
                        stake_infos = stake_results.scalars().all()
                        
                        # Create a lookup for stake info by player ID
                        for stake_info in stake_infos:
                            stake_info_by_player[stake_info.player_id] = stake_info
                    
                    session = updated_session

                if session.session_type != "swiss":
                    team_a_display_names = [session.sign_ups[user_id] for user_id in session.team_a]
                    team_b_display_names = [session.sign_ups[user_id] for user_id in session.team_b]
                    seating_order = await generate_seating_order(bot, session)
                else:
                    sign_ups_list = list(session.sign_ups.keys())
                    random.shuffle(sign_ups_list)  # This shuffles the list in-place
                    seating_order = [session.sign_ups[user_id] for user_id in sign_ups_list]
                    new_sign_ups = {user_id: session.sign_ups[user_id] for user_id in sign_ups_list}
                    await db_session.execute(update(DraftSession)
                                        .where(DraftSession.session_id == session.session_id)
                                        .values(sign_ups=new_sign_ups))

                # Create the embed message for displaying the teams and seating order
                embed = discord.Embed(
                    title=f"Draft-{session.draft_id} is Ready!",
                    description=f"**DRAFTMANCER SESSION:➡️ [JOIN DRAFT HERE]({session.draft_link})** ⬅️\n\n" +
                                "Host of Draftmancer must manually adjust seating as per below. **TURN OFF RANDOM SEATING SETTING IN DRAFTMANCER**" +
                                "\n\n**AFTER THE DRAFT**, select Create Chat Rooms and Post Pairings" +
                                "\nPost Pairings will post in the created draft-chat room",
                    color=discord.Color.dark_gold() if session.session_type == "swiss" else discord.Color.blue()
                )
                
                if session.session_type != 'swiss':
                    # Change to Team Red and Team Blue with emojis
                    embed.add_field(name="🔴 Team Red" if session.session_type == "random" or session.session_type == "staked" else f"{session.team_a_name}", 
                                    value="\n".join(team_a_display_names), 
                                    inline=True)
                    embed.add_field(name="🔵 Team Blue" if session.session_type == "random" or session.session_type == "staked" else f"{session.team_b_name}", 
                                    value="\n".join(team_b_display_names), 
                                    inline=True)
                
                embed.add_field(name="Seating Order", value=" -> ".join(seating_order), inline=False)
                
                # Add stakes information for staked drafts
                if self.session_type == "staked" and updated_session:
                    from utils import get_formatted_stake_pairs
                    
                    stake_lines, total_stakes = await get_formatted_stake_pairs(
                        updated_session.session_id,
                        updated_session.sign_ups
                    )
                    
                    # Format with bold names for the initial display
                    formatted_lines = []
                    for line in stake_lines:
                        parts = line.split(': ')
                        names = parts[0].split(' vs ')
                        formatted_lines.append(f"**{names[0]}** vs **{names[1]}**: {parts[1]}")
                    
                    # Add the stakes field to the embed
                    if formatted_lines:
                        embed.add_field(
                            name=f"Stakes (Total: {total_stakes} tix)",
                            value="\n".join(formatted_lines),
                            inline=False
                        )
                # If this is a staked draft, add a button to explain the stake calculations
                if self.session_type == "staked":
                    # Create a view with the stake calculation button
                    stake_view = discord.ui.View(timeout=None)
                    
                    # Add the existing buttons from self.children to the new view
                    for item in self.children:
                        if isinstance(item, discord.ui.Button):
                            # Clone the button with the same properties
                            button_copy = CallbackButton(
                                label=item.label,
                                style=item.style,
                                custom_id=item.custom_id,
                                custom_callback=item.custom_callback
                            )
                            
                            # Set disabled state based on button type
                            if item.custom_id == f"create_rooms_pairings_{self.draft_session_id}" or item.custom_id == f"cancel_draft_{self.draft_session_id}":
                                button_copy.disabled = False
                            else:
                                button_copy.disabled = True
                                
                            stake_view.add_item(button_copy)
                    
                    # Add our new stake calculation button
                    stake_view.add_item(StakeCalculationButton(session.session_id))
                    
                    # Use the new view instead of self
                    await interaction.response.edit_message(embed=embed, view=stake_view)
                    return  # Return early to avoid the default response
                
                # Iterate over the view's children (buttons) to update their disabled status
                for item in self.children:
                    if isinstance(item, discord.ui.Button):
                        # Enable "Create Rooms" and "Cancel Draft" buttons
                        if item.custom_id == f"create_rooms_pairings_{self.draft_session_id}" or item.custom_id == f"cancel_draft_{self.draft_session_id}":
                            item.disabled = False
                        else:
                            # Disable all other buttons
                            item.disabled = True
                await db_session.commit()

        # Respond with the embed and updated view
        await interaction.response.edit_message(embed=embed, view=self)
        if session.tracked_draft and session.premade_match_id is not None:
            await check_weekly_limits(interaction, session.premade_match_id, session.session_type, session.session_id)

    async def team_assignment_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = await get_draft_session(self.draft_session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return

        user_id = str(interaction.user.id)  
        custom_id = button.custom_id
        user_name = interaction.user.display_name

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
        session = await get_draft_session(self.draft_session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return

        # Show confirmation dialog
        confirm_view = CancelConfirmationView(self.bot, self.draft_session_id, interaction.user.display_name)
        await interaction.response.send_message("Are you sure you want to cancel this draft?", view=confirm_view, ephemeral=True)    

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
        
        try:
            await interaction.response.defer()
        except discord.errors.NotFound:
            print("Creating Rooms: Interaction not found or expired.")
            del PROCESSING_ROOMS_PAIRINGS[session_id]
            return

        try:    
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
                    guild = interaction.guild
                    bot = interaction.client
                    if session.session_type != "swiss":
                        await calculate_pairings(session, db_session)
                    else:
                        state_to_save, match_counter = await calculate_pairings(session, db_session)
                        session.match_counter = match_counter
                        session.swiss_matches = state_to_save
                    if session.session_type == "random":
                        await update_player_stats_for_draft(session.session_id, guild)
                    
                    for child in self.children:
                        if isinstance(child, discord.ui.Button) and child.label == "Create Rooms & Post Pairings":
                            child.disabled = True
                            break

                    # Execute tasks to create chat channels
                    if self.session_type == "swiss":
                        sign_ups_list = list(session.sign_ups.keys())
                        all_members = [guild.get_member(int(user_id)) for user_id in sign_ups_list]
                        #all_members = [session.sign_ups[user_id] for user_id in sign_ups_list]
                        session.draft_chat_channel = str(await self.create_team_channel(guild, "Draft", all_members))
                        draft_chat_channel = guild.get_channel(int(session.draft_chat_channel))
                    elif self.session_type != "test":
                        team_a_members = [guild.get_member(int(user_id)) for user_id in session.team_a if guild.get_member(int(user_id))]
                        team_b_members = [guild.get_member(int(user_id)) for user_id in session.team_b if guild.get_member(int(user_id))]
                        all_members = team_a_members + team_b_members

                        session.draft_chat_channel = str(await self.create_team_channel(guild, "Draft", all_members, session.team_a, session.team_b))
                        await self.create_team_channel(guild, "Red-Team", team_a_members, session.team_a, session.team_b)
                        await self.create_team_channel(guild, "Blue-Team", team_b_members, session.team_a, session.team_b)

                        # Fetch the channel object using the ID
                        draft_chat_channel = guild.get_channel(int(session.draft_chat_channel))
                    else:
                        draft_chat_channel = guild.get_channel(int(session.draft_channel_id))
                        session.draft_chat_channel = session.draft_channel_id
                    draft_summary_embed = await generate_draft_summary_embed(bot, session.session_id)
                    

                    sign_up_tags = ' '.join([f"<@{user_id}>" for user_id in session.sign_ups.keys()])

                    await draft_chat_channel.send(f"Pairings posted below. Good luck in your matches! {sign_up_tags}")

                    if session.session_type == "staked":
                        # Create a view with the stake calculation button
                        from views import StakeCalculationButton
                        stake_view = discord.ui.View(timeout=None)
                        stake_view.add_item(StakeCalculationButton(session.session_id))
                        
                        # Send the draft summary with the button
                        draft_summary_message = await draft_chat_channel.send(embed=draft_summary_embed, view=stake_view)
                    else:
                        draft_summary_message = await draft_chat_channel.send(embed=draft_summary_embed)

                    if self.session_type != "test":
                        await draft_summary_message.pin()
                    session.draft_summary_message_id = str(draft_summary_message.id)
                    if self.session_type != "test":
                        await draft_summary_message.pin()
                    session.draft_summary_message_id = str(draft_summary_message.id)


                    draft_channel_id = int(session.draft_channel_id) 
                    original_message_id = int(session.message_id)
                    draft_channel = interaction.client.get_channel(draft_channel_id)

                    # Fetch the channel and delete the message
                    
                    if draft_channel:
                        try:
                            original_message = await draft_channel.fetch_message(original_message_id)
                            await original_message.delete()
                        except discord.NotFound:
                            print(f"Original message {original_message_id} not found in channel {draft_channel_id}.")
                        except discord.HTTPException as e:
                            print(f"Failed to delete message {original_message_id}: {e}")

                    session.deletion_time = datetime.now() + timedelta(days=7)

                    await db_session.commit()
                # Execute Post Pairings
                await post_pairings(bot, guild, session.session_id)
                from livedrafts import create_live_draft_summary
                await create_live_draft_summary(bot, session.session_id)
                await interaction.followup.send("Pairings posted.", ephemeral=True)

                draft_link = session.draft_link
                if draft_link:      
                    from datacollections import DraftLogManager
                    manager = DraftLogManager(session.session_id, draft_link, session.draft_id, session.session_type, session.cube)
                    asyncio.create_task(manager.keep_draft_session_alive())
                else:
                    print("Draft link not found in database.")
        except discord.errors.NotFound:
            print("Interaction not found or expired.")
        except Exception as e:
            print(f"An error occurred while creating rooms: {e}")
        finally:
            del PROCESSING_ROOMS_PAIRINGS[session_id]

    async def calculate_and_store_stakes(self, interaction: discord.Interaction, draft_session):
        """Calculate and store stakes for a staked draft session."""
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                # Get all stake info records for this session
                stake_stmt = select(StakeInfo).where(StakeInfo.session_id == draft_session.session_id)
                results = await db_session.execute(stake_stmt)
                stake_info_records = results.scalars().all()
                
                # Build stakes dictionary
                stakes_dict = {record.player_id: record.max_stake for record in stake_info_records}
                
                # Use the StakeCalculator to calculate stake pairs
                min_stake = draft_session.min_stake or 10
                stake_pairs = StakeCalculator.calculate_stakes(
                    draft_session.team_a, 
                    draft_session.team_b, 
                    stakes_dict,
                    min_stake
                )
                
                # First, clear any existing assigned stakes to avoid duplications
                for record in stake_info_records:
                    record.assigned_stake = None
                    record.opponent_id = None
                    db_session.add(record)
                
                # Now update with the new calculated stakes
                processed_pairs = set()  # Track which pairs we've handled
                
                for pair in stake_pairs:
                    # Create a unique identifier for this pair 
                    # Sort the player IDs but keep the amount separate
                    pair_id = (tuple(sorted([pair.player_a_id, pair.player_b_id])), pair.amount)
                    
                    # Skip if we've already processed this exact pairing
                    if pair_id in processed_pairs:
                        continue
                    processed_pairs.add(pair_id)
                    
                    # Update player A's stake info
                    player_a_stmt = select(StakeInfo).where(and_(
                        StakeInfo.session_id == draft_session.session_id,
                        StakeInfo.player_id == pair.player_a_id
                    ))
                    player_a_result = await db_session.execute(player_a_stmt)
                    player_a_info = player_a_result.scalars().first()
                    
                    if player_a_info:
                        # Update existing record
                        player_a_info.opponent_id = pair.player_b_id
                        player_a_info.assigned_stake = pair.amount
                        db_session.add(player_a_info)
                    
                    # Update player B's stake info
                    player_b_stmt = select(StakeInfo).where(and_(
                        StakeInfo.session_id == draft_session.session_id,
                        StakeInfo.player_id == pair.player_b_id
                    ))
                    player_b_result = await db_session.execute(player_b_stmt)
                    player_b_info = player_b_result.scalars().first()
                    
                    if player_b_info:
                        # Update existing record
                        player_b_info.opponent_id = pair.player_a_id
                        player_b_info.assigned_stake = pair.amount
                        db_session.add(player_b_info)
                
                # Commit the changes
                await db_session.commit()

    async def create_team_channel(self, guild, team_name, team_members, team_a=None, team_b=None):
        from config import get_config, is_special_guild

        config = get_config(guild.id)
        draft_category = discord.utils.get(guild.categories, name=config["categories"]["draft"])
        voice_category = None
        if is_special_guild(guild.id) and "voice" in config["categories"]:
            voice_category = discord.utils.get(guild.categories, name=config["categories"]["voice"])
        
        session = await get_draft_session(self.draft_session_id)
        if not session:
            print("Draft session not found.")
            return
        channel_name = f"{team_name}-Chat-{session.draft_id}"

        # Get the admin role from config instead of hardcoding role names
        admin_role_name = config["roles"].get("admin")
        admin_role = discord.utils.get(guild.roles, name=admin_role_name) if admin_role_name else None
        
        # Basic permissions overwrites for the channel
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True)
        }

        # For team-specific channels (Red-Team or Blue-Team)
        if team_name in ["Red-Team", "Blue-Team"]:
            if admin_role:
                # First, give all admins access by default
                overwrites[admin_role] = discord.PermissionOverwrite(read_messages=True)
                
                # Then, for each admin who is participating in the draft, adjust permissions individually
                for member in admin_role.members:
                    # If admin is on the opposite team, deny access to this team's channel
                    if (team_name == "Red-Team" and member.id in team_b) or (team_name == "Blue-Team" and member.id in team_a):
                        overwrites[member] = discord.PermissionOverwrite(read_messages=False)
        else:
            # For the "Draft-chat" channel, add read permissions for admin role
            if admin_role:
                overwrites[admin_role] = discord.PermissionOverwrite(read_messages=True)

        # Add team members with read permission (this overrides any role-based permissions)
        for member in team_members:
            overwrites[member] = discord.PermissionOverwrite(read_messages=True)
        
        # Create the channel with the specified overwrites
        channel = await guild.create_text_channel(name=channel_name, overwrites=overwrites, category=draft_category)
        self.channel_ids.append(channel.id)
        if session.premade_match_id and team_name != "Draft" and session.session_type == "premade":
            # Construct voice channel name
            voice_channel_name = f"{team_name}-Voice-{session.draft_id}"
            # Create the voice channel with the same permissions as the text channel
            voice_channel = await guild.create_voice_channel(name=voice_channel_name, overwrites=overwrites, category=voice_category)
            # Store the voice channel ID
            self.channel_ids.append(voice_channel.id)

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

async def generate_ready_check_embed(ready_check_status, sign_ups):
    # Define a function to convert user IDs to their names using the sign_ups dictionary
    def get_names(user_ids):
        return "\n".join(sign_ups.get(user_id, "Unknown user") for user_id in user_ids) or "None"

    # Generate the embed with fields for "Ready", "Not Ready", and "No Response"
    embed = discord.Embed(title="Ready Check Initiated", description="Please indicate if you are ready.", color=discord.Color.gold())
    embed.add_field(name="Ready", value=get_names(ready_check_status['ready']), inline=False)
    embed.add_field(name="Not Ready", value=get_names(ready_check_status['not_ready']), inline=False)
    embed.add_field(name="No Response", value=get_names(ready_check_status['no_response']), inline=False)
    
    return embed

class ReadyCheckView(discord.ui.View):
    def __init__(self, draft_session_id):
        super().__init__(timeout=None)
        self.draft_session_id = draft_session_id
        # Append the session ID to each custom_id to make it unique
        self.ready_button.custom_id = f"ready_check_ready_{self.draft_session_id}"
        self.not_ready_button.custom_id = f"ready_check_not_ready_{self.draft_session_id}"

    @discord.ui.button(label="Ready", style=discord.ButtonStyle.green, custom_id="placeholder_ready")
    async def ready_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.handle_ready_not_ready_interaction(interaction, "ready")

    @discord.ui.button(label="Not Ready", style=discord.ButtonStyle.red, custom_id="placeholder_not_ready")
    async def not_ready_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.handle_ready_not_ready_interaction(interaction, "not_ready")

    async def handle_ready_not_ready_interaction(self, interaction: discord.Interaction, status):
        session = sessions.get(self.draft_session_id)
        if not session:
            await interaction.response.send_message("Session data is missing.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        if user_id not in session['no_response'] and user_id not in session['ready'] and user_id not in session['not_ready']:
            await interaction.response.send_message("You are not authorized to interact with this button.", ephemeral=True)
            return

        # Update the ready check status
        for state in ['ready', 'not_ready', 'no_response']:
            if user_id in session[state]:
                session[state].remove(user_id)
        session[status].append(user_id)

        # Update the session status in the database if necessary
        # await update_draft_session(self.draft_session_id, session)
        draft_session = await get_draft_session(self.draft_session_id)
        # Generate the updated embed
        embed = await generate_ready_check_embed(session, draft_session.sign_ups)

        # Update the message
        await interaction.response.edit_message(embed=embed, view=self)
        



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
            if session.session_type != "premade":
                await update_draft_message(bot, session_id=session.session_id)
            else:
                await PersistentView.update_team_view(interaction)

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
            match_id=match_result.id,  
            match_number=match_result.match_number,
            label=f"Match {match_result.match_number} Results",
            style=discord.ButtonStyle.secondary,
            row=None  
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
        view = View(timeout=None)
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
                stmt = select(MatchResult, DraftSession).join(DraftSession).where(
                    MatchResult.session_id == self.session_id,
                    MatchResult.match_number == self.match_number
                )
                result = await session.execute(stmt)
                match_result, draft_session = result.first()
                if match_result:
                    # Update the match result based on the selection
                    match_result.player1_wins = player1_wins
                    match_result.player2_wins = player2_wins
                    if winner_indicator != '0':  
                        winner_id = match_result.player1_id if winner_indicator == '1' else match_result.player2_id
                    match_result.winner_id = winner_id

                    await session.commit()  # Commit the changes to the database
                    
                    if draft_session and draft_session.session_type == "random":
                        await update_player_stats_and_elo(match_result)
                   
        await update_draft_summary_message(self.bot, self.session_id)
        from livedrafts import update_live_draft_summary
        await update_live_draft_summary(self.bot, self.session_id)
        if draft_session.session_type != "test":
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
                    
                    # Determine which team won
                    winning_team_emoji = ""
                    if match_result.winner_id:
                        # Get draft session to check which team the winner belongs to
                        draft_session_result = await session.execute(select(DraftSession).filter_by(session_id=draft_session_id))
                        draft_session = draft_session_result.scalar_one_or_none()
                        
                        if draft_session and match_result.winner_id in draft_session.team_a:
                            winning_team_emoji = "🔴 "  # Red emoji for Team A
                        elif draft_session and match_result.winner_id in draft_session.team_b:
                            winning_team_emoji = "🔵 "  # Blue emoji for Team B
                    
                    # Update the field with the winning team emoji
                    updated_value = f"{winning_team_emoji}**Match {match_result.match_number}**\n{player1_name}: {match_result.player1_wins} wins\n{player2_name}: {match_result.player2_wins} wins"
                    
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
            # Fetch draft session
            draft_session_result = await session.execute(select(DraftSession).filter_by(session_id=draft_session_id))
            draft_session = draft_session_result.scalar_one_or_none()
            
            # Fetch MatchResults associated with this specific pairing_message_id
            stmt = select(MatchResult).where(
                MatchResult.session_id == draft_session_id,
                MatchResult.pairing_message_id == pairing_message_id
            )
            result = await session.execute(stmt)
            match_results = result.scalars().all()

            for match_result in match_results:
                # Determine button style based on winning team
                button_style = discord.ButtonStyle.secondary  # Default style if no winner
                
                if match_result.winner_id:
                    if draft_session and match_result.winner_id in draft_session.team_a:
                        button_style = discord.ButtonStyle.danger  # Red for Team A
                    elif draft_session and match_result.winner_id in draft_session.team_b:
                        button_style = discord.ButtonStyle.blurple  # Blue for Team B
                    else:
                        button_style = discord.ButtonStyle.grey  # Fallback
                
                # Create a button with the appropriate style
                button = MatchResultButton(
                    bot=bot,
                    session_id=draft_session_id,
                    match_id=match_result.id,
                    match_number=match_result.match_number,
                    label=f"Match {match_result.match_number} Results",
                    style=button_style
                )

                # Add the newly created button to the view
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
    logger.info(f"Starting update for draft message with session ID: {session_id}")

    # Fetch draft session
    draft_session = await get_draft_session(session_id)
    if not draft_session:
        logger.error("Failed to fetch draft session for updating the message.")
        return

    channel_id = int(draft_session.draft_channel_id)
    message_id = int(draft_session.message_id)
    logger.info(f"Fetched draft session. Channel ID: {channel_id}, Message ID: {message_id}")

    # Fetch channel
    channel = bot.get_channel(channel_id)
    if not channel:
        logger.error(f"Channel with ID {channel_id} not found.")
        return

    try:
        # Fetch message
        message = await channel.fetch_message(message_id)
        logger.info(f"Fetched message with ID: {message_id} from channel {channel_id}")

        # Update embed with sign-ups
        embed = message.embeds[0]  # Assuming there's at least one embed in the message
        sign_up_count = len(draft_session.sign_ups)
        sign_ups_field_name = f"Sign-Ups ({sign_up_count}):"
        
        # For staked drafts, fetch the stake information
        stake_info_by_player = {}
        if draft_session.session_type == "staked":
            async with AsyncSessionLocal() as db_session:
                async with db_session.begin():
                    stake_stmt = select(StakeInfo).where(StakeInfo.session_id == session_id)
                    results = await db_session.execute(stake_stmt)
                    stake_infos = results.scalars().all()
                    
                    # Create a lookup for stake info by player ID
                    for stake_info in stake_infos:
                        stake_info_by_player[stake_info.player_id] = stake_info.max_stake
        
        # Create sign-ups string with stake amounts for staked drafts
        if draft_session.session_type == "staked":
            sign_ups_list = []
            for user_id, display_name in draft_session.sign_ups.items():
                # Default to "Not set" if no stake has been set yet
                stake_amount = stake_info_by_player.get(user_id, "Not set")
                sign_ups_list.append((user_id, display_name, stake_amount))
            
            # Sort by stake amount (highest first)
            # Convert "Not set" to -1 for sorting purposes
            def sort_key(item):
                stake = item[2]
                return -1 if stake == "Not set" else stake
            
            sign_ups_list.sort(key=sort_key, reverse=True)
            
            # Format with stakes
            formatted_sign_ups = []
            for user_id, display_name, stake_amount in sign_ups_list:
                if stake_amount == "Not set":
                    formatted_sign_ups.append(f"❌ Not set: {display_name}")
                else:
                    formatted_sign_ups.append(f"✅ {stake_amount} tix: {display_name}")
            
            sign_ups_str = '\n'.join(formatted_sign_ups) if formatted_sign_ups else 'No players yet.'
        else:
            sign_ups_str = '\n'.join(draft_session.sign_ups.values()) if draft_session.sign_ups else 'No players yet.'
        
        embed.set_field_at(0, name=sign_ups_field_name, value=sign_ups_str, inline=False)
        await message.edit(embed=embed)
        logger.info(f"Successfully updated message for session ID: {session_id}")

    except Exception as e:
        logger.exception(f"Failed to update message for session {session_id}. Error: {e}")


class CancelConfirmationView(discord.ui.View):
    def __init__(self, bot, draft_session_id, user_display_name):
        super().__init__(timeout=60)  # 60 second timeout
        self.bot = bot
        self.draft_session_id = draft_session_id
        self.user_display_name = user_display_name

    @discord.ui.button(label="Yes, Cancel Draft", style=discord.ButtonStyle.danger)
    async def confirm_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        # Disable buttons
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        
        # Get session to proceed with cancellation
        session = await get_draft_session(self.draft_session_id)
        if not session:
            await interaction.followup.send("The draft session could not be found.", ephemeral=True)
            return
        
        # First, announce the cancellation in the channel
        channel = self.bot.get_channel(int(session.draft_channel_id))
        if channel:
            await channel.send(f"User **{self.user_display_name}** has cancelled the draft.")
        
        # Then delete the message
        if channel:
            try:
                message = await channel.fetch_message(int(session.message_id))
                await message.delete()
            except Exception as e:
                print(f"Failed to delete draft message: {e}")
        
        # Remove from database
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                await db_session.delete(session)
                await db_session.commit()
        
        await interaction.followup.send("The draft has been canceled.", ephemeral=True)

    @discord.ui.button(label="No, Keep Draft", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        # Disable buttons
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Draft cancellation aborted.", view=self)

class StakeModal(discord.ui.Modal):
    def __init__(self, draft_session_id: str, draft_link: str = None, *args, **kwargs):
        super().__init__(title="Enter Your Maximum Stake", *args, **kwargs)
        self.draft_session_id = draft_session_id
        self.draft_link = draft_link
        
        self.stake_input = discord.ui.InputText(
            label="Maximum Stake (tix)",
            placeholder="Enter maximum amount you're willing to bet",
            required=True
        )
        self.add_item(self.stake_input)
    
    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        
        # Parse the stake amount
        try:
            max_stake = int(self.stake_input.value)
            if max_stake < 10:
                await interaction.response.send_message("Minimum stake is 10 tix. Please try again.", ephemeral=True)
                return
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)
            return
        
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Get the draft session to check min stake
                draft_stmt = select(DraftSession).where(DraftSession.session_id == self.draft_session_id)
                draft_result = await session.execute(draft_stmt)
                draft_session = draft_result.scalars().first()
                
                if not draft_session:
                    await interaction.response.send_message("Draft session not found.", ephemeral=True)
                    return
                
                min_stake = draft_session.min_stake or 10
                if max_stake < min_stake:
                    await interaction.response.send_message(f"Minimum stake for this draft is {min_stake} tix.", ephemeral=True)
                    return
                
                # Check if a stake record already exists for this player
                stake_stmt = select(StakeInfo).where(and_(
                    StakeInfo.session_id == self.draft_session_id,
                    StakeInfo.player_id == user_id
                ))
                stake_result = await session.execute(stake_stmt)
                stake_info = stake_result.scalars().first()
                
                if stake_info:
                    # Update existing stake
                    stake_info.max_stake = max_stake
                else:
                    # Create new stake record
                    stake_info = StakeInfo(
                        session_id=self.draft_session_id,
                        player_id=user_id,
                        max_stake=max_stake
                    )
                    session.add(stake_info)
                
                await session.commit()
        
        # Create a response that includes both the stake confirmation and draft link
        signup_message = f"You've set your maximum stake to {max_stake} tix."
        if self.draft_link:
            signup_message += f"\n\nYou are now signed up. Join Here: {self.draft_link}"
        
        await interaction.response.send_message(signup_message, ephemeral=True)
        
        # Update the draft message to reflect the new list of sign-ups
        await update_draft_message(interaction.client, self.draft_session_id)

class StakeCalculationButton(discord.ui.Button):
    def __init__(self, session_id):
        super().__init__(
            label="How Stakes Were Calculated",
            style=discord.ButtonStyle.secondary,
            custom_id=f"stake_calculation_{session_id}"
        )
        self.session_id = session_id
        
    async def callback(self, interaction: discord.Interaction):
        """Show a detailed explanation of how stakes were calculated"""
        await interaction.response.defer(ephemeral=True)  # Use ephemeral to show just to the user who clicked
        
        # Fetch the draft session
        draft_session = await get_draft_session(self.session_id)
        if not draft_session:
            await interaction.followup.send("Draft session not found.", ephemeral=True)
            return
            
        # Fetch all stake info records
        async with AsyncSessionLocal() as session:
            # Get the original stake inputs
            stake_stmt = select(StakeInfo).where(StakeInfo.session_id == self.session_id)
            results = await session.execute(stake_stmt)
            stake_infos = results.scalars().all()
            
            # Create mapping of player IDs to max stakes
            max_stakes = {info.player_id: info.max_stake for info in stake_infos}
            
            # Create mapping of player IDs to display names
            player_names = {player_id: draft_session.sign_ups.get(player_id, "Unknown") for player_id in max_stakes.keys()}
            
            # Create the explanation embed
            embed = discord.Embed(
                title="Stake Calculation Explanation",
                description="Here's how the stakes were calculated for this draft:",
                color=discord.Color.gold()
            )
            
            # Step 1: Show the input stakes
            team_a_stakes = []
            for player_id in draft_session.team_a:
                if player_id in max_stakes:
                    team_a_stakes.append(f"{player_names[player_id]}: {max_stakes[player_id]} tix")
            
            team_b_stakes = []
            for player_id in draft_session.team_b:
                if player_id in max_stakes:
                    team_b_stakes.append(f"{player_names[player_id]}: {max_stakes[player_id]} tix")
            
            embed.add_field(
                name="Step 1: Input Max Stakes",
                value="**Team A:**\n" + "\n".join(team_a_stakes) + "\n\n**Team B:**\n" + "\n".join(team_b_stakes),
                inline=False
            )
            
            # Step 2: Explain the sorting
            embed.add_field(
                name="Step 2: Sort By Stake Amount",
                value=(
                    "Players on each team are sorted by their max stake (highest first).\n"
                    "Then players from each team are matched against each other in order."
                ),
                inline=False
            )
            
            # Step 3: Show the initial pairing results
            primary_pairings = []
            team_a_sorted = sorted([(p, max_stakes.get(p, 0)) for p in draft_session.team_a if p in max_stakes], 
                                  key=lambda x: x[1], reverse=True)
            team_b_sorted = sorted([(p, max_stakes.get(p, 0)) for p in draft_session.team_b if p in max_stakes], 
                                  key=lambda x: x[1], reverse=True)
            
            leftovers_a = []
            leftovers_b = []
            
            for i in range(min(len(team_a_sorted), len(team_b_sorted))):
                player_a, stake_a = team_a_sorted[i]
                player_b, stake_b = team_b_sorted[i]
                
                bet_amount = min(stake_a, stake_b)
                pairings_text = (
                    f"{player_names[player_a]} ({stake_a} tix) vs {player_names[player_b]} ({stake_b} tix)\n"
                    f"= {bet_amount} tix (minimum of the two)"
                )
                
                # Check for leftovers
                if stake_a > bet_amount:
                    leftovers_a.append((player_a, stake_a - bet_amount))
                    pairings_text += f"\n{player_names[player_a]} has {stake_a - bet_amount} tix remaining"
                    
                if stake_b > bet_amount:
                    leftovers_b.append((player_b, stake_b - bet_amount))
                    pairings_text += f"\n{player_names[player_b]} has {stake_b - bet_amount} tix remaining"
                    
                primary_pairings.append(pairings_text)
            
            # Add any unmatched players from first pass
            if len(team_a_sorted) > len(team_b_sorted):
                for i in range(len(team_b_sorted), len(team_a_sorted)):
                    player_a, stake_a = team_a_sorted[i]
                    leftovers_a.append((player_a, stake_a))
                    primary_pairings.append(f"{player_names[player_a]} ({stake_a} tix) - Unmatched in initial pairing")
            elif len(team_b_sorted) > len(team_a_sorted):
                for i in range(len(team_a_sorted), len(team_b_sorted)):
                    player_b, stake_b = team_b_sorted[i]
                    leftovers_b.append((player_b, stake_b))
                    primary_pairings.append(f"{player_names[player_b]} ({stake_b} tix) - Unmatched in initial pairing")
            
            embed.add_field(
                name="Step 3: Initial Pairings",
                value="\n\n".join(primary_pairings) if primary_pairings else "No initial pairings",
                inline=False
            )
            
            # Step 4: Secondary pairings
            if leftovers_a or leftovers_b:
                secondary_pairings = []
                
                leftovers_a.sort(key=lambda x: x[1], reverse=True)
                leftovers_b.sort(key=lambda x: x[1], reverse=True)
                
                i = 0
                while i < len(leftovers_a) and i < len(leftovers_b):
                    player_a, remaining_a = leftovers_a[i]
                    player_b, remaining_b = leftovers_b[i]
                    
                    secondary_bet = min(remaining_a, remaining_b)
                    if secondary_bet >= draft_session.min_stake:
                        secondary_pairings.append(
                            f"{player_names[player_a]} ({remaining_a} tix) vs {player_names[player_b]} ({remaining_b} tix)\n"
                            f"= {secondary_bet} tix"
                        )
                        
                        # Check for still more leftovers
                        if remaining_a > secondary_bet:
                            leftovers_a.append((player_a, remaining_a - secondary_bet))
                        if remaining_b > secondary_bet:
                            leftovers_b.append((player_b, remaining_b - secondary_bet))
                    else:
                        secondary_pairings.append(
                            f"{player_names[player_a]} ({remaining_a} tix) vs {player_names[player_b]} ({remaining_b} tix)\n"
                            f"Not enough for a bet (below minimum of {draft_session.min_stake} tix)"
                        )
                        
                    i += 1
                
                embed.add_field(
                    name="Step 4: Secondary Pairings (Using Leftover Stakes)",
                    value="\n\n".join(secondary_pairings) if secondary_pairings else "No secondary pairings",
                    inline=False
                )
                
                # Step 5: Any unused stakes
                unused_stakes = []
                for player_id, amount in leftovers_a + leftovers_b:
                    if amount >= draft_session.min_stake:
                        unused_stakes.append(f"{player_names[player_id]}: {amount} tix")
                
                if unused_stakes:
                    embed.add_field(
                        name="Step 5: Unused Stakes",
                        value="\n".join(unused_stakes),
                        inline=False
                    )
            
            # Send the explanation
            await interaction.followup.send(embed=embed, ephemeral=True)