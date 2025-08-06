import discord
import asyncio
import random
import pytz
from datetime import datetime, timedelta
from discord import SelectOption
from discord.ui import Button, View, Select, select
from config import TEST_MODE_ENABLED
from draft_organization.stake_calculator import calculate_stakes_with_strategy
from services.draft_setup_manager import DraftSetupManager, ACTIVE_MANAGERS
from session import StakeInfo, AsyncSessionLocal, get_draft_session, DraftSession, MatchResult
from models import SignUpHistory
from sqlalchemy import update, select, and_
from sqlalchemy.orm import selectinload
from helpers.utils import get_cube_thumbnail_url
from utils import calculate_pairings, get_formatted_stake_pairs, generate_draft_summary_embed ,post_pairings, generate_seating_order, fetch_match_details, split_into_teams, update_draft_summary_message, check_and_post_victory_or_draw, update_player_stats_and_elo, check_weekly_limits, update_player_stats_for_draft
from cube_views.CubeSelectionView import CubeUpdateSelectionView
from loguru import logger

READY_CHECK_COOLDOWNS = {}
PROCESSING_ROOMS_PAIRINGS = {}
PROCESSING_TEAMS_CREATION = {}
sessions = {}

def split_content_for_embed(content, include_header=False, max_length=1000):
    """
    Helper function to split content into chunks that fit within Discord's embed field value limits.
    
    Args:
        content: Either a list of strings or a single string with newlines
        include_header: If True, keeps the first line in all chunks
        max_length: Max character length per chunk (default 1000)
        
    Returns:
        List of content chunks, each under max_length characters
    """
    # Handle both list input and string input
    if isinstance(content, str):
        lines = content.split('\n')
    else:
        lines = content
        
    if not lines:
        return []
        
    chunks = []
    header = lines[0] if include_header else None
    content_lines = lines[1:] if include_header else lines
    
    current_chunk = header if include_header else ""
    
    # Helper to check if adding a line would exceed the limit
    def would_exceed_limit(chunk, line):
        if not chunk:
            return False
        if line:
            return len(chunk + '\n' + line) > max_length
        return len(chunk) > max_length
    
    for line in content_lines:
        if not current_chunk:
            current_chunk = line
            continue
            
        if would_exceed_limit(current_chunk, line):
            # Current chunk is full
            chunks.append(current_chunk)
            current_chunk = header if header else ""
            
            # Add the current line to the new chunk
            if current_chunk:
                current_chunk += '\n' + line
            else:
                current_chunk = line
        else:
            # Add line to current chunk
            current_chunk += '\n' + line
    
    # Add the last chunk if it has content
    if current_chunk:
        chunks.append(current_chunk)
        
    return chunks

def add_links_to_embed_safely(embed, links, base_name, team_color=""):
    """
    Helper function to add links to an embed, splitting them into multiple fields if needed
    to avoid exceeding Discord's 1024 character limit per field.
    
    Args:
        embed: The discord.Embed object to add fields to
        links: List of link strings to add
        base_name: Base name for the embed field
        team_color: Optional color indicator ('red', 'blue', or '') for emoji prefixing
    """
    if not links:
        return
    
    # Join links into a single string for processing
    content = "\n".join(links)
    
    # If all links fit in one field, add them directly
    if len(content) <= 1000:
        emoji = "🔴 " if team_color == "red" else "🔵 " if team_color == "blue" else ""
        embed.add_field(
            name=f"{emoji}{base_name}",
            value=content,
            inline=False
        )
        return
    
    # Otherwise, split into chunks and add as multiple fields
    chunks = split_content_for_embed(links)
    emoji = "🔴 " if team_color == "red" else "🔵 " if team_color == "blue" else ""
    
    for i, chunk in enumerate(chunks):
        suffix = "" if i == 0 else f" (part {i+1})"
        value = chunk if isinstance(chunk, str) else "\n".join(chunk)
        embed.add_field(
            name=f"{emoji}{base_name}{suffix}",
            value=value,
            inline=False
        )


class PersistentView(discord.ui.View):

    # AUTO_PAIRINGS_TASKS = {}  # session_id -> task

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
        if self.session_type != "premade":
            self._add_signup_buttons()

        self._add_shared_buttons()

        if self.session_type == "winston":
            self._add_winston_specific_buttons()
        elif self.session_type == "premade":
            self._add_premade_buttons()
        else:
            self._add_generic_buttons()

        if self.session_type == "staked":
            self.add_item(BetCapToggleButton(self.draft_session_id))

        if self.session_type != "test":
            self._add_button("Ready Check", "green", "ready_check", self.ready_check_callback)
            self._add_button("Create Rooms & Post Pairings", "primary", "create_rooms_pairings", self.create_rooms_pairings_callback, disabled=True)

        self._apply_stage_button_disabling()


    def _add_button(self, label, style, custom_id_suffix, callback, **kwargs):
        button = self.create_button(label, style, f"{custom_id_suffix}_{self.draft_session_id}", callback, **kwargs)
        self.add_item(button)


    def _add_signup_buttons(self):
        self._add_button("Sign Up", "green", "sign_up", self.sign_up_callback)
        self._add_button("Cancel Sign Up", "red", "cancel_sign_up", self.cancel_sign_up_callback)


    def _add_shared_buttons(self):
        self._add_button("Cancel Draft", "grey", "cancel_draft", self.cancel_draft_callback)
        self._add_button("Remove User", "grey", "remove_user", self.remove_user_button_callback)
        self._add_button("Update Cube", "blurple", "update_cube", self.update_cube_callback)


    def _add_winston_specific_buttons(self):
        self._add_button("Start Draft", "green", "start_draft", self.start_draft_callback)


    def _add_premade_buttons(self):
        self._add_button(self.team_a_name, "green", "Team_A", self.team_assignment_callback)
        self._add_button(self.team_b_name, "red", "Team_B", self.team_assignment_callback)
        self._add_button("Generate Seating Order", "primary", "generate_seating", self.randomize_teams_callback)


    def _add_generic_buttons(self):
        if self.session_type == "swiss":
            self._add_button("Generate Seating Order", "blurple", "randomize_teams", self.randomize_teams_callback)
        elif self.session_type in {"test", "schedule"}:
            # "Cancel Draft" and "Remove User" already added via shared buttons
            return
        else:
            self._add_button("Create Teams", "blurple", "randomize_teams", self.randomize_teams_callback)

        if self.session_type == "staked" and self.session_stage != "teams":
            self._add_button("How Bets Work 💰", "green", "explain_stakes", self.explain_stakes_callback)
            
        # Add test button only if global test mode is enabled
        if TEST_MODE_ENABLED:
            self._add_button("🧪 Add Test Users", "grey", "add_test_users", self.add_test_users_callback)


    def _apply_stage_button_disabling(self):
        if self.session_stage == "teams":
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    if item.custom_id == f"create_rooms_pairings_{self.draft_session_id}":
                        item.disabled = False
                    elif item.custom_id == f"cancel_draft_{self.draft_session_id}":
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
            
    # Maximum number of test users to add
    NUM_TEST_USERS_TO_ADD = 6
    
    async def add_test_users_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Add test users to the draft for testing purposes, up to NUM_TEST_USERS_TO_ADD."""
        # Only allow admins to use this feature
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only server administrators can use this test feature.", ephemeral=True)
            return
                    
        logger.info(f"Adding test users to draft {self.draft_session_id}")
        
        # Fetch the current draft session to ensure it's up to date
        draft_session = await get_draft_session(self.draft_session_id)
        if not draft_session:
            logger.error(f"Draft session {self.draft_session_id} not found")
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return
            
        if draft_session.session_stage == "teams":
            logger.warning(f"Cannot add test users after teams have been created for draft {self.draft_session_id}")
            await interaction.response.send_message("Cannot add test users after teams have been created.", ephemeral=True)
            return
        
        # First acknowledge the interaction so it doesn't time out
        await interaction.response.defer(ephemeral=True)

        # Generate unique user IDs starting from a high number to avoid conflicts
        start_id = 900000000000000000
        
        # Create fake users with long names to test the character limit
        test_names = [
            "SuperLongUserName_Testing_Character_Limits_One",
            "AnotherVeryLongUsername_For_Testing_Two", 
            "ThirdLongUsername_With_Extra_Characters_Three",
            "FourthLongUsername_To_Test_UI_Rendering_Four",
            "FifthLongUsername_With_Special_Chars_Five",
            "SixthLongUsername_Testing_Overflow_Six",
            "SeventhLongUsername_With_Multiple_Words_Seven",
            "EighthLongUsername_That_Is_Extremely_Long_Eight",
            "NinthLongUsername_To_Ensure_We_Hit_The_Limit_Nine",
        ]
        
        # Get existing sign-ups
        sign_ups = draft_session.sign_ups or {}
        original_count = len(sign_ups)
        
        # Calculate how many users to add (limited by NUM_TEST_USERS_TO_ADD)
        # If we already have users, only add enough to reach NUM_TEST_USERS_TO_ADD total
        users_to_add = max(0, self.NUM_TEST_USERS_TO_ADD - original_count)
        
        # Limit to available test names
        users_to_add = min(users_to_add, len(test_names))
        
        if users_to_add <= 0:
            await interaction.followup.send(f"Already have {original_count} users (limit is {self.NUM_TEST_USERS_TO_ADD}).", ephemeral=True)
            return
            
        logger.info(f"Adding {users_to_add} test users to draft {self.draft_session_id}")
        
        # Create a simpler direct approach - add test users up to the limit
        fake_users = {}
        for i in range(users_to_add):
            user_id = str(start_id + i)
            name = test_names[i]
            fake_users[user_id] = name
            logger.info(f"Generated test user: {name} with ID {user_id}")
            
        # Add our new users to existing sign-ups
        sign_ups.update(fake_users)
        logger.info(f"Updated sign_ups from {original_count} to {len(sign_ups)} users")
        
        # Database updates - handle stake info for staked drafts
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                # Update the draft session with new sign-ups
                await db_session.execute(
                    update(DraftSession).
                    where(DraftSession.session_id == self.draft_session_id).
                    values(sign_ups=sign_ups)
                )
                
                # For staked drafts, create stake info entries
                if draft_session.session_type == "staked" and fake_users:
                    logger.info(f"Adding stake info for {len(fake_users)} test users")
                    for user_id, name in fake_users.items():
                        stake_amount = random.randint(5, 20) * 10
                        stake_info = StakeInfo(
                            session_id=draft_session.session_id,
                            player_id=user_id,
                            max_stake=stake_amount,
                            assigned_stake=0,
                            is_capped=random.choice([True, False])
                        )
                        db_session.add(stake_info)
                        logger.info(f"Added stake info for {name}: max stake {stake_amount}")
                
                await db_session.commit()
        
        # Update the draft message to reflect the new list of sign-ups
        await update_draft_message(interaction.client, self.draft_session_id)
        
        # Re-fetch to get the updated session data
        updated_session = await get_draft_session(self.draft_session_id)
        if not updated_session:
            logger.error(f"Failed to fetch updated draft session {self.draft_session_id}")
            await interaction.followup.send("Error: Could not refresh draft data after adding test users", ephemeral=True)
            return
        
        # Report success to the user
        if len(fake_users) > 0:
            success_msg = f"Added {len(fake_users)} test users to the draft (total: {len(sign_ups)})."
            if draft_session.session_type == "staked":
                success_msg += " Each user has different stake amounts and preferences."
            
            logger.info(f"Test users added successfully: {success_msg}")
            await interaction.followup.send(success_msg, ephemeral=True)
        else:
            await interaction.followup.send(f"No additional test users were added. The draft already has {len(sign_ups)} users (limit is {self.NUM_TEST_USERS_TO_ADD}).", ephemeral=True)


    async def sign_up_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        
        # Get the guild config for the interaction's guild
        from config import get_config
        config = get_config(interaction.guild_id)
        roles_config = config.get("roles", {})
        timeout_role_name = roles_config.get("timeout")
        
        # Check if the user has the timeout role
        if timeout_role_name and discord.utils.get(interaction.user.roles, name=timeout_role_name):
            await interaction.response.send_message(
                "You are ineligible to join a queue due to an infraction (Leaving Draft Early/Unpaid Debts). Message a Mod for more details.",
                ephemeral=True
            )
            return
        
        # Check if user has Draftmancer role
        has_draftmancer_role = discord.utils.get(interaction.user.roles, name="Draftmancer") is not None
        
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
        # Initialize draftmancer_role_users if it doesn't exist
        draftmancer_role_users = draft_session.draftmancer_role_users or []
        

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
            # Handle staked drafts differently - show dropdown first before adding to sign_ups
            if self.session_type == "staked":
                # Create and send the stake options view
                stake_options_view = StakeOptionsView(
                    draft_session_id=self.draft_session_id,
                    draft_link=draft_session.draft_link,
                    user_display_name=interaction.user.display_name,
                    min_stake=draft_session.min_stake,
                    has_draftmancer_role=has_draftmancer_role  
                )
                await interaction.response.send_message(
                    f"Min Bet for queue is {draft_session.min_stake}. Select your max bet:",
                    view=stake_options_view,
                    ephemeral=True
                )
                return
            
            # For non-staked drafts, add them to sign_ups now        
            sign_ups[user_id] = interaction.user.display_name
            display_name = str(interaction.user.display_name)
            
            # Record the signup event in history
            await SignUpHistory.record_signup_event(
                session_id=self.draft_session_id,
                user_id=user_id,
                display_name=display_name,
                action="join",
                guild_id=str(interaction.guild_id)
            )
            
            # If user has Draftmancer role, add them to draftmancer_role_users
            if has_draftmancer_role and display_name not in draftmancer_role_users:
                draftmancer_role_users.append(display_name)

            # Check if this is the 6th person to sign up AND we haven't pinged yet
            should_ping = False
            # 30 min delay
            now = datetime.now()
            ping_cooldown = draft_session.draft_start_time + timedelta(minutes=30)
            
            if len(sign_ups) in (5, 7) and not draft_session.should_ping and now > ping_cooldown:
                should_ping = True

            # Start an asynchronous database session
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    # Update values based on whether we need to ping
                    values_to_update = {
                        "sign_ups": sign_ups,
                        "draftmancer_role_users": draftmancer_role_users
                    }
                    if should_ping:
                        values_to_update["should_ping"] = True

                    # Reset the inactivity timer when a user signs up (if still in initial queue)
                    # Skip resetting deletion_time for guild ID 1229863996929216686
                    if not draft_session.session_stage and draft_session.guild_id != "1229863996929216686":
                        values_to_update["deletion_time"] = datetime.now() + timedelta(minutes=180)

                    # Update the draft session in the database
                    await session.execute(
                        update(DraftSession).
                        where(DraftSession.session_id == self.draft_session_id).
                        values(**values_to_update)
                    )
                    await session.commit()

            # After committing, re-fetch the draft session to work with updated data
            draft_session_updated = await get_draft_session(self.draft_session_id)
            if not draft_session_updated:
                print("Failed to fetch updated draft session after sign-up.")
                return
                    
            # Confirm signup with draft link
            draft_link = draft_session_updated.get_draft_link_for_user(interaction.user.display_name)
            signup_confirmation_message = f"You are now signed up. Join Here: {draft_link}"
            await interaction.response.send_message(signup_confirmation_message, ephemeral=True)

            # Send ping if needed (6th player and haven't pinged yet)
            if should_ping:
                # Get the drafter role from the config
                from config import get_config
                guild_config = get_config(interaction.guild_id)
                drafter_role_name = guild_config["roles"]["drafter"]
                
                # Find the role in the guild
                guild = interaction.guild
                drafter_role = discord.utils.get(guild.roles, name=drafter_role_name)
                
                if drafter_role:
                    # Get the channel where the draft message is
                    channel = await interaction.client.fetch_channel(draft_session_updated.draft_channel_id)
                    if channel:
                        player_count = len(sign_ups)
                        await channel.send(f"{player_count} Players in queue! {drafter_role.mention}")

            # Update the draft message to reflect the new list of sign-ups
            await update_draft_message(interaction.client, self.draft_session_id)
            
            if self.session_type == "winston":
                if len(sign_ups) == 2:
                    sign_up_tags = ' '.join([f"<@{user_id}>" for user_id in draft_session_updated.sign_ups.keys()])
                    guild = self.bot.get_guild(int(interaction.guild_id))
                    channel = discord.utils.get(guild.text_channels, name="winston-draft")
                    await channel.send(f"Winston Draft Ready. Good luck in your match! {sign_up_tags}")
                else:
                    guild = interaction.guild
                    message_link = f"https://discord.com/channels/{draft_session_updated.guild_id}/{draft_session_updated.draft_channel_id}/{draft_session_updated.message_id}"
                    channel = discord.utils.get(guild.text_channels, name="cube-draft-open-play")
                    await channel.send(f"**{interaction.user.display_name}** is looking for an opponent for a **Winston Draft**. [Join Here!]({message_link})")
                    
    async def cancel_sign_up_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        draft_session = await get_draft_session(self.draft_session_id)
        if not draft_session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return
        
        sign_ups = draft_session.sign_ups or {}
        draftmancer_role_users = draft_session.draftmancer_role_users or []
        user_id = str(interaction.user.id)
        display_name = str(interaction.user.display_name)
        
        if user_id not in sign_ups:
            # User is not signed up; inform them
            await interaction.response.send_message("You are not signed up!", ephemeral=True)
        else:
            # User is canceling their sign-up
            del sign_ups[user_id]
            
            # Record the leave event in history
            await SignUpHistory.record_signup_event(
                session_id=self.draft_session_id,
                user_id=user_id,
                display_name=display_name,
                action="leave",
                guild_id=str(interaction.guild_id)
            )
            
            # Remove user from draftmancer_role_users if present
            if display_name in draftmancer_role_users:
                draftmancer_role_users.remove(display_name)

            # Start an asynchronous database session
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    # Directly update the 'sign_ups' and 'draftmancer_role_users' of the draft session
                    await session.execute(
                        update(DraftSession).
                        where(DraftSession.session_id == self.draft_session_id).
                        values(
                            sign_ups=sign_ups,
                            draftmancer_role_users=draftmancer_role_users
                        )
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
    
    async def update_cube_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Callback for the Update Cube button - Shows a selection view for choosing a new cube"""
        try:
            draft_session = await get_draft_session(self.draft_session_id)
            if not draft_session:
                await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
                return
            
            # Create the appropriate cube selection view based on session type
            cube_selection = CubeUpdateSelectionView(draft_session.session_type)
            
            async def custom_cube_callback(select_interaction):
                try:
                    # Get the selected cube choice
                    cube_choice = select_interaction.data["values"][0]
                    
                    # If the cube choice is the same as the current cube, no need to update
                    if draft_session.cube == cube_choice:
                        await select_interaction.response.send_message(
                            f"The draft is already using the {cube_choice} cube.",
                            ephemeral=True
                        )
                        return
                    
                    # Confirm the cube update
                    await select_interaction.response.send_message(
                        f"Updating draft to use cube: {cube_choice}...", 
                        ephemeral=True
                    )
                    
                    # Try to get the existing manager
                    manager = DraftSetupManager.get_active_manager(draft_session.session_id)
                    
                    if not manager:
                        # Even without a manager, we can still update the database
                        logger.warning(f"No active draft manager found for session {draft_session.session_id}. "
                                      f"Updating database only.")
                        
                        # Update database and message without manager
                        async with AsyncSessionLocal() as db_session:
                            async with db_session.begin():
                                await db_session.execute(
                                    update(DraftSession)
                                    .where(DraftSession.session_id == draft_session.session_id)
                                    .values(cube=cube_choice)
                                )
                                await db_session.commit()
                                
                        # Update the draft message with the new cube
                        await update_draft_message(select_interaction.client, draft_session.session_id)
                        
                        await select_interaction.followup.send(
                            f"The draft has been updated to use cube: {cube_choice} (Note: Draft has already started, "
                            f"so the cube won't be updated in Draftmancer).",
                            ephemeral=True
                        )
                        return
                    
                    # If we have a manager, update the cube through it
                    success = await manager.update_cube(cube_choice)
                    
                    if success:
                        # Update the database with the new cube name
                        async with AsyncSessionLocal() as db_session:
                            async with db_session.begin():
                                await db_session.execute(
                                    update(DraftSession)
                                    .where(DraftSession.session_id == draft_session.session_id)
                                    .values(cube=cube_choice)
                                )
                                await db_session.commit()
                        
                        # Update the draft message with the new cube info
                        await update_draft_message(select_interaction.client, draft_session.session_id)
                        
                        await select_interaction.followup.send(
                            f"The draft has been updated to use cube: {cube_choice}",
                            ephemeral=True
                        )
                    else:
                        await select_interaction.followup.send(
                            f"Failed to update the cube in Draftmancer. Please try again later.",
                            ephemeral=True
                        )
                
                except Exception as e:
                    logger.exception(f"Error in cube update callback: {e}")
                    await select_interaction.followup.send(
                        f"An error occurred while updating the cube: {str(e)}",
                        ephemeral=True
                    )
            
            # Replace the original callback with our custom one
            cube_selection.cube_select.callback = custom_cube_callback
            
            await interaction.response.send_message(
                f"Select a new cube for this draft (currently using {draft_session.cube}):", 
                view=cube_selection, 
                ephemeral=True
            )
            
        except Exception as e:
            logger.exception(f"Error in update_cube_callback: {e}")
            await interaction.response.send_message(
                f"An error occurred while preparing the cube update: {str(e)}",
                ephemeral=True
            )
    
    async def ready_check_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if this draft session is in cooldown
        current_time = datetime.now()
        cooldown_end_time = READY_CHECK_COOLDOWNS.get(self.draft_session_id)

        # Fetch the session data from the database
        session = await get_draft_session(self.draft_session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return        
        
        sign_up_count = len(session.sign_ups)
        if sign_up_count not in (6,8,10):
            await interaction.response.send_message(
                f"Ready check only available with 6, 8, or 10 players. Currently {sign_up_count} players in queue."
            )
            return

        if cooldown_end_time and current_time < cooldown_end_time:
            # Calculate remaining cooldown time in seconds
            remaining_seconds = int((cooldown_end_time - current_time).total_seconds())
            await interaction.response.send_message(
                f"Ready check is on cooldown. Please wait {remaining_seconds} seconds before initiating another check.",
                ephemeral=True
            )
            return
        
        # Set a new cooldown for this draft session (60 seconds)
        READY_CHECK_COOLDOWNS[self.draft_session_id] = current_time + timedelta(seconds=60)
        
        # Schedule the cooldown to be removed after 60 seconds
        asyncio.create_task(self.remove_cooldown(self.draft_session_id))
        


        user_id = str(interaction.user.id)
        if user_id not in session.sign_ups:
            await interaction.response.send_message("You are not registered in the draft session.", ephemeral=True)
            return
        
        # Create a dictionary to store the initial ready check status
        ready_check_status = {
            "ready": [user_id],  # Add the initiator to ready immediately
            "not_ready": [],
            "no_response": [uid for uid in session.sign_ups.keys() if uid != user_id]  # All others except initiator
        }

        # Save this status in a global sessions dictionary
        sessions[self.draft_session_id] = ready_check_status
        logger.info(f"✅ Ready check initiated - registered session ID {self.draft_session_id} in sessions dictionary")
        logger.debug(f"Sessions dictionary now contains {list(sessions.keys())}")

        # Disable the "Ready Check" button
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id.endswith("ready_check"):
                item.disabled = True
                break

        # Generate the initial embed with personalized links
        embed = await generate_ready_check_embed(ready_check_status=ready_check_status, sign_ups=session.sign_ups, draft_link=session.draft_link, draft_session=session)
        
        # Create the view with the buttons
        view = ReadyCheckView(self.draft_session_id)

        # Send the initial ready check message
        main_message = await interaction.response.send_message(embed=embed, view=view, ephemeral=False)

        # Construct a message that mentions all users who need to respond to the ready check
        user_mentions = ' '.join([f"<@{user_id}>" for user_id in session.sign_ups.keys()])
        mention_message = f"Ready Check Initiated {user_mentions}"

        # Send the mention message as a follow-up to ensure it gets sent after the embed
        await interaction.followup.send(mention_message, ephemeral=False)

        # asyncio.create_task(self.cleanup_ready_check(self.draft_session_id))

    async def remove_cooldown(self, draft_session_id):
        await asyncio.sleep(60)  # Wait for 60 seconds
        if draft_session_id in READY_CHECK_COOLDOWNS:
            del READY_CHECK_COOLDOWNS[draft_session_id]

    # async def cleanup_ready_check(self, draft_session_id):
    #     await asyncio.sleep(1800)  # Wait for 30 minutes
    #     try:
    #         if draft_session_id in sessions:
    #             logger.warning(f"⚠️ Removing session {draft_session_id} from sessions dictionary due to timeout")
    #             del sessions[draft_session_id]  # Clean up the session data
    #             logger.debug(f"Sessions dictionary after cleanup: {list(sessions.keys())}")
    #         else:
    #             logger.info(f"Session {draft_session_id} already removed from sessions dictionary")
    #     except Exception as e:
    #         logger.error(f"Failed during ready check cleanup: {e}")

    async def _validate_team_creation_request(self, interaction: discord.Interaction, session_id: str, user_id: str):
        """Validate initial conditions for team creation"""
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                stmt = select(DraftSession).where(DraftSession.session_id == session_id)
                result = await db_session.execute(stmt)
                session = result.scalars().first()

                if not session:
                    await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
                    return False, None
                
                # Check if user is in the queue
                if user_id not in session.sign_ups:
                    await interaction.response.send_message(
                        "You are not eligible to create teams as you are not in the queue.", 
                        ephemeral=True
                    )
                    return False, None
                    
                return True, session

    async def _validate_staked_draft_requirements(self, interaction: discord.Interaction, session_id: str):
        """Validate staked draft specific requirements"""
        # Check if a ready check has been performed
        ready_check_performed = session_id in sessions
        logger.info(f"Ready check verification: session_id={session_id}, in sessions dict={ready_check_performed}")
        
        if not ready_check_performed:
            logger.warning(f"❌ Ready check verification failed for session {session_id}")
            await interaction.followup.send(
                "You must perform a Ready Check before creating teams for a money draft.",
                ephemeral=True
            )
            return False
        
        # Check that all players have set their stakes
        from utils import get_missing_stake_players
        missing_players = await get_missing_stake_players(session_id)
        if missing_players:
            # Get display names for the missing players
            guild = interaction.client.get_guild(int(interaction.guild_id))
            missing_names = []
            for pid in missing_players:
                member = guild.get_member(int(pid))
                if member:
                    missing_names.append(member.display_name)
            
            # Format error message
            players_str = ", ".join(missing_names)
            await interaction.followup.send(
                f"Cannot create teams yet. The following players need to set their stakes: {players_str}",
                ephemeral=True
            )
            return False
            
        return True

    async def randomize_teams_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = interaction.client
        session_id = self.draft_session_id
        user_id = str(interaction.user.id)

        # Check for race condition - is someone already creating teams?
        if PROCESSING_TEAMS_CREATION.get(session_id):
            await interaction.response.send_message(
                "Teams are already being created. Please wait.", 
                ephemeral=True
            )
            return
        
        # Mark as processing to prevent race conditions
        PROCESSING_TEAMS_CREATION[session_id] = True

        try:
            logger.info(f"Create teams initiated for session {self.draft_session_id} of type {self.session_type}")
            
            # Validate initial request
            is_valid, session = await self._validate_team_creation_request(interaction, session_id, user_id)
            if not is_valid:
                return
            
            # Defer the response early since team creation might take time
            await interaction.response.defer()
            
            logger.info(f"Create teams - checking sessions dict: {list(sessions.keys())}")
            
            # Validate staked draft requirements if needed
            if self.session_type == "staked":
                if not await self._validate_staked_draft_requirements(interaction, session_id):
                    return
                
            async with AsyncSessionLocal() as db_session:
                async with db_session.begin():
                    stmt = select(DraftSession).where(DraftSession.session_id == session_id)
                    result = await db_session.execute(stmt)
                    session = result.scalars().first()

                    if not session:
                        await interaction.followup.send("The draft session could not be found.", ephemeral=True)
                        return
                        
                    if len(session.sign_ups) % 2 != 0:
                        await interaction.followup.send("There must be an even number of players to fire.")
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
                        
                        if session_id in sessions:
                            logger.info(f"✅ Teams created - removing ready check data for session {session_id}")
                            del sessions[session_id]
                            logger.debug(f"Sessions dictionary after cleanup: {list(sessions.keys())}")

                        # Now that teams exist, calculate stakes for staked drafts
                        stake_pairs = []
                        stake_info_by_player = {}
                        
                        if self.session_type == "staked" and updated_session and updated_session.team_a and updated_session.team_b:
                            # Load player preferences for all participants
                            all_players = updated_session.team_a + updated_session.team_b
                            
                            # Get cap info from database
                            from preference_service import get_players_bet_capping_preferences
                            cap_info = await get_players_bet_capping_preferences(all_players, guild_id=str(interaction.guild_id))
                            
                            # Calculate and store stakes
                            await self.calculate_and_store_stakes(interaction, updated_session, cap_info)
                            
                            # Fetch the calculated stakes for display
                            stake_stmt = select(StakeInfo).where(StakeInfo.session_id == session_id)
                            stake_results = await db_session.execute(stake_stmt)
                            stake_infos = stake_results.scalars().all()
                            
                            # Create a lookup for stake info by player ID
                            for stake_info in stake_infos:
                                stake_info_by_player[stake_info.player_id] = stake_info
                        
                        session = updated_session

                if session.session_type != "swiss":
                    sign_ups_list = list(session.sign_ups.keys())
                    if session.session_type == "premade":
                        seating_order = await generate_seating_order(bot, session)
                    else:
                        seating_order = [session.sign_ups[user_id] for user_id in sign_ups_list]
                    team_a_display_names = [session.sign_ups[user_id] for user_id in session.team_a]
                    team_b_display_names = [session.sign_ups[user_id] for user_id in session.team_b]
                    random.shuffle(team_a_display_names)
                    random.shuffle(team_b_display_names)
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
                    description=f"**Chosen Cube: [{session.cube}]"
                                f"(https://cubecobra.com/cube/list/{session.cube})**\n\n" 
                                "Host of Draftmancer must manually adjust seating as per below. \n**TURN OFF RANDOM SEATING SETTING IN DRAFTMANCER**" 
                                "\n\n**AFTER THE DRAFT**, select Create Chat Rooms and Post Pairings" 
                                "\nPairings will post in the created draft-chat room",
                    color=discord.Color.dark_gold() if session.session_type == "swiss" else discord.Color.blue()
                )
                
                # Add personalized draft links for each user
                user_links = []
                for user_id, display_name in session.sign_ups.items():
                    personalized_link = session.get_draft_link_for_user(display_name)
                    user_links.append(f"**{display_name}**: [Draft Link]({personalized_link})")
                
                # Use the helper function to safely add links to the embed
                add_links_to_embed_safely(embed, user_links, "Your Personalized Draft Links")
                
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
                        add_links_to_embed_safely(
                            embed, 
                            formatted_lines, 
                            f"Bets (Total: {total_stakes} tix)"
                        )
                        
                # Create the new channel embed for team announcements
                channel_embed = discord.Embed(
                    title="Teams have been formed. Seating Order Below!",
                    description=f"**Chosen Cube: [{session.cube}]"
                                f"(https://cubecobra.com/cube/list/{session.cube})**\n\n",
                    color=discord.Color.dark_gold() if session.session_type == "swiss" else discord.Color.green()
                )
                
                # Add personalized draft links for each user in the channel embed, separated by teams
                # This helps avoid hitting Discord's 1024 character limit per field
                team_a_links = []
                team_b_links = []
                
                for user_id, display_name in session.sign_ups.items():
                    personalized_link = session.get_draft_link_for_user(display_name)
                    # Use shorter format to save characters
                    link_entry = f"**{display_name}**: [Draft Link]({personalized_link})"
                    
                    # Sort into appropriate team
                    if session.session_type == 'swiss':
                        # For swiss, just put everyone in team_a_links
                        team_a_links.append(link_entry)
                    else:
                        # For team drafts, split by team
                        if user_id in session.team_a:
                            team_a_links.append(link_entry)
                        elif user_id in session.team_b:
                            team_b_links.append(link_entry)
                
                # Add team A links
                if team_a_links:
                    team_name = "Team Red" if session.session_type in ["random", "staked"] else session.team_a_name
                    team_name = team_name if team_name else "Team A"
                    add_links_to_embed_safely(
                        channel_embed, 
                        team_a_links, 
                        f"{team_name} Draft Links", 
                        "red" if session.session_type in ["random", "staked"] else ""
                    )
                
                # Add team B links
                if team_b_links:
                    team_name = "Team Blue" if session.session_type in ["random", "staked"] else session.team_b_name
                    team_name = team_name if team_name else "Team B"
                    add_links_to_embed_safely(
                        channel_embed, 
                        team_b_links, 
                        f"{team_name} Draft Links", 
                        "blue" if session.session_type in ["random", "staked"] else ""
                    )
                
                # channel_embed.add_field(name="Automatic Rooms Creation", value=countdown_message, inline=False)

                # Add team information to channel embed
                # if session.session_type != 'swiss':
                #     channel_embed.add_field(name="🔴 Team Red" if session.session_type == "random" or session.session_type == "staked" else f"{session.team_a_name}", 
                #                 value="\n".join(team_a_display_names), 
                #                 inline=True)
                #     channel_embed.add_field(name="🔵 Team Blue" if session.session_type == "random" or session.session_type == "staked" else f"{session.team_b_name}", 
                #                 value="\n".join(team_b_display_names), 
                #                 inline=True)
                
                # Add seating order to channel embed
                channel_embed.add_field(name="Seating Order", value=" -> ".join(seating_order), inline=False)
                
                # # Add stakes information to channel embed for staked drafts
                # if self.session_type == "staked" and updated_session:
                #     # We already have the formatted lines from above
                #     if formatted_lines:
                #         channel_embed.add_field(
                #             name=f"Stakes (Total: {total_stakes} tix)",
                #             value="\n".join(formatted_lines),
                #             inline=False
                #         )
                
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
                            if item.custom_id == f"create_rooms_pairings_{self.draft_session_id}":
                                button_copy.disabled = False
                            elif item.custom_id == f"cancel_draft_{self.draft_session_id}":
                                button_copy.disabled = False
                            else:
                                button_copy.disabled = True
                                
                            stake_view.add_item(button_copy)
                    
                    stake_view.add_item(StakeCalculationButton(session.session_id))
                    
                    # Use the new view instead of self
                    try:
                        await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=self)
                    except Exception as e:
                        logger.error(f"Failed to update draft message: {e}")
                    
                    # Send the channel announcement after responding to the interaction
                    await interaction.channel.send(embed=channel_embed)
                    
                    # Return early to avoid the default response
                    await db_session.commit()
                    try:    
                        # Look for an existing manager
                        manager = DraftSetupManager.get_active_manager(self.draft_session_id)
                        
                        if manager:
                            logger.info(f"TEAMS CREATED: Found existing manager for session {self.draft_session_id}")
                            logger.info(f"TEAMS CREATED: Manager state - Seating set: {manager.seating_order_set}, "
                                        f"Users count: {manager.users_count}, Expected count: {manager.expected_user_count}")
                            
                            # Make sure bot instance is set properly
                            manager.set_bot_instance(interaction.client)
                            logger.info(f"Set bot instance on manager to ensure Discord messaging works")
                            
                            # Manager exists, force a check of session stage
                            logger.info("Check Session Stage from randomize teams callback for staked draft")
                            await manager.check_session_stage_and_organize()
                            
                            # Also force a refresh of users to ensure accurate count
                            if manager.sio.connected:
                                await manager.sio.emit('getUsers')
                        else:
                            logger.info(f"No existing manager for {self.draft_session_id}")

                    except Exception as e:
                        # Log the error but don't disrupt the normal flow
                        logger.exception(f"Error setting up draft manager: {e}")

                    if session.tracked_draft and session.premade_match_id is not None:
                        await check_weekly_limits(interaction, session.premade_match_id, session.session_type, session.session_id)
                    return
                
                # Iterate over the view's children (buttons) to update their disabled status
                for item in self.children:
                    if isinstance(item, discord.ui.Button):
                        # Enable "Create Rooms" and "Cancel Draft" buttons
                        if item.custom_id == f"create_rooms_pairings_{self.draft_session_id}":
                            item.disabled = False
                        elif item.custom_id == f"cancel_draft_{self.draft_session_id}":
                            item.disabled = False
                        else:
                            # Disable all other buttons
                            item.disabled = True
                await db_session.commit()
    
            # Respond with the embed and updated view
            try:
                await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=self)
            except Exception as e:
                logger.error(f"Failed to update draft message: {e}")
        
        
            # Send the channel announcement after responding to the interaction
            await interaction.channel.send(embed=channel_embed)

        except Exception as e:
            logger.exception(f"Error in randomize_teams_callback: {e}")
            try:
                await interaction.followup.send(
                    "An error occurred while creating teams. Please try again.",
                    ephemeral=True
                )
            except:
                pass
            # Setup draft manager (moved inside main try block to ensure cleanup)
            try:
                manager = DraftSetupManager.get_active_manager(self.draft_session_id)
                
                if manager:
                    logger.info(f"TEAMS CREATED: Found existing manager for session {self.draft_session_id}")
                    logger.info(f"TEAMS CREATED: Manager state - Seating set: {manager.seating_order_set}, "
                                f"Users count: {manager.users_count}, Expected count: {manager.expected_user_count}")
                            
                    # Make sure bot instance is set 
                    manager.set_bot_instance(interaction.client)
                    logger.info(f"Set bot instance on manager to ensure Discord messaging works")
                            
                    # Manager exists, force a check of session stage
                    logger.info("Check session from randomize teams normal draft")
                    await manager.check_session_stage_and_organize()
                    
                    # Also force a refresh of users to ensure accurate count
                    if manager.sio.connected:
                        await manager.sio.emit('getUsers')
                else:
                    logger.info(f"DraftSetupManager not found for {self.draft_session_id}")

            except Exception as e:
                # Log the error but don't disrupt the normal flow
                logger.exception(f"Error triggering seating order process: {e}")

        finally:
            # Always clean up the processing flag
            if session_id in PROCESSING_TEAMS_CREATION:
                del PROCESSING_TEAMS_CREATION[session_id]

        # Handle weekly limits check outside the try block since we need session data
        try:
            if session.tracked_draft and session.premade_match_id is not None:
                await check_weekly_limits(interaction, session.premade_match_id, session.session_type, session.session_id)
        except Exception as e:
            logger.exception(f"Error checking weekly limits: {e}")
        
    async def explain_stakes_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Explain how the stake system works"""
        embed = discord.Embed(
            title="How the Dynamic Bet System Works",
            description=(
                "The dynamic bet system allows players to bet different amounts based on their personal preferences "
                "to ensure all players can bet what they are comfortable with."
            ),
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="Core Principles",
            value=(
                "• **Max Bet Protection**: You will never be allocated more than your maximum bet amount\n"
                "• **Team Formation**: Teams are created randomly FIRST, then bets are allocated\n"
                "• **Flexibility**: The system adapts to different betting situations using two methods"
            ),
            inline=False
        )
        
        embed.add_field(
            name="Process Overview",
            value=(
                "The betting process works in two phases:\n"
                "1. **Allocation Phase**: Determine how much each player will bet\n"
                "2. **Bet Matching Phase**: Create player-to-player betting pairs"
            ),
            inline=False
        )
        
        embed.add_field(
            name="Bet Capping Option",
            value=(
                "• Players can choose \"capped\" (🧢) or \"uncapped\" (🏎️)\n"
                "• Capped bets are limited to the highest bet on the opposing team\n"
                "• This is applied before any calculations occur"
            ),
            inline=False
        )
        
        embed.add_field(
            name="Determining Method Selection",
            value=(
                "To decide which allocation method to use, the system:\n"
                "• Calculates each team's minimum bet requirements:\n"
                "  - For bets ≤50 tix: Uses the full bet amount for that drafter\n"
                "  - For bets >50 tix: Uses 50 tix as the minimum for that drafter\n"
                "• Compares each team's total bet capacity to the opposing team's minimum requirements\n"
                "• If both teams pass, use the \"Tiered\" approach\n"
                "• If either team fails this check, switches to \"Proportional\" Approach"
            ),
            inline=False
        )
        
        embed.add_field(
            name="The Allocation Phase",
            value=(
                "**Initial Team Analysis (Common to Both Methods)**\n"
                "• Identify Min Team (lower total bets) and Max Team (higher total bets)\n"
                "• 100% of a drafter's max bet is allocated to Min Team players\n\n"
                "**Max Team Allocation Methods:**"
            ),
            inline=False
        )
        
        embed.add_field(
            name="Tiered Approach (Primary Method)",
            value=(
                "Used when both teams have sufficient capacity to meet minimum requirements:\n"
                "• Players betting ≤50 tix get 100% bet allocation first\n"
                "• Remaining capacity is distributed proportionally to higher bets\n"
                "• Prioritizes filling all 10/20/50 bets first before filling bets >50 tix"
            ),
            inline=False
        )
        
        embed.add_field(
            name="Proportional Approach (Fallback Method)",
            value=(
                "Used when minimum bet requirements cannot be met with the Tiered Approach:\n"
                "• Players with minimum bets get 100% of their bet allocated\n"
                "• Other players receive proportional allocations based on a bet score:\n"
                "  - Bet score = remaining Min Team capacity ÷ remaining Max Team capacity\n"
                "  - Allocation = individual bet × bet score (rounded to nearest 10)"
            ),
            inline=False
        )
        
        embed.add_field(
            name="The Bet Matching Phase",
            value=(
                "**1. Identical Allocation Matching (First Priority)**"
                "• Groups players by their allocation amounts\n"
                "• Matches players with identical allocations first\n"
                "• Creates perfect 1:1 matches requiring only one transaction per pair\n"
                "**2. Smart Matching Algorithm**"
                "• Uses a scoring system to determine optimal pairings\n"
                "• Prioritizes matches that completely fulfill a player's allocation\n"
                "• Balances bet sizes to minimize the total number of transactions"
            ),
            inline=False
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

                    
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
        # Get the guild config for the interaction's guild
        from config import get_config
        config = get_config(interaction.guild_id)
        roles_config = config.get("roles", {})
        timeout_role_name = roles_config.get("timeout")
        
        # Check if the user has the timeout role
        if timeout_role_name and discord.utils.get(interaction.user.roles, name=timeout_role_name):
            await interaction.response.send_message(
                "You are ineligible due to an infraction (Leaving Draft Early/Unpaid Debts). Message a Mod for more details.",
                ephemeral=True
            )
            return

        session = await get_draft_session(self.draft_session_id)
        if not session:
            await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
            return

        # Show confirmation dialog
        confirm_view = CancelConfirmationView(self.bot, self.draft_session_id, interaction.user.display_name)
        await interaction.response.send_message("Are you sure you want to cancel this draft?", view=confirm_view, ephemeral=True)    
    
    async def start_draft_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = interaction.client
        session_id = self.draft_session_id
        
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                stmt = select(DraftSession).where(DraftSession.session_id == session_id)
                result = await db_session.execute(stmt)
                session = result.scalars().first()

                if not session:
                    await interaction.response.send_message("The draft session could not be found.", ephemeral=True)
                    return
                    
                if len(session.sign_ups) != 2:
                    await interaction.response.send_message("Winston draft requires exactly 2 players.", ephemeral=True)
                    return

                # Update the session object
                session.teams_start_time = datetime.now()
                session.deletion_time = datetime.now() + timedelta(hours=4)
                session.session_stage = 'teams'
                
                # Create teams for winston draft
                await split_into_teams(bot, session.session_id)
                
                # Re-fetch session to get updated teams
                updated_session = await get_draft_session(self.draft_session_id)
                session = updated_session

                # Get display names for participants
                sign_ups_list = list(session.sign_ups.keys())
                seating_order = [session.sign_ups[user_id] for user_id in sign_ups_list]
                team_a_display_names = [session.sign_ups[user_id] for user_id in session.team_a]
                team_b_display_names = [session.sign_ups[user_id] for user_id in session.team_b]
                
                # Create embed for the draft teams and links
                embed = discord.Embed(
                    title=f"Winston Draft-{session.draft_id} is Ready!",
                    description=f"**Chosen Cube: [{session.cube}]"
                                f"(https://cubecobra.com/cube/list/{session.cube})**\n\n" 
                                "Host of Draftmancer must manually adjust seating as per below.",
                    color=discord.Color.blue()
                )
                
                # Add personalized draft links for each user
                user_links = []
                for user_id, display_name in session.sign_ups.items():
                    personalized_link = session.get_draft_link_for_user(display_name)
                    user_links.append(f"**{display_name}**: [Your Draft Link]({personalized_link})")
                
                embed.add_field(
                    name="Your Personalized Draft Links",
                    value="\n".join(user_links),
                    inline=False
                )
                
                # Add team information
                embed.add_field(name="🔴 Team Red", 
                                value="\n".join(team_a_display_names), 
                                inline=True)
                embed.add_field(name="🔵 Team Blue", 
                                value="\n".join(team_b_display_names), 
                                inline=True)
                
                embed.add_field(name="Seating Order", value=" -> ".join(seating_order), inline=False)
                
                # Create the channel announcement embed
                channel_embed = discord.Embed(
                    title="Winston Draft Teams have been formed!",
                    description=f"**Chosen Cube: [{session.cube}]"
                                f"(https://cubecobra.com/cube/list/{session.cube})**\n\n",
                    color=discord.Color.green()
                )
                
                # Add personalized draft links to channel embed
                channel_embed.add_field(
                    name="Your Personalized Draft Links",
                    value="\n".join(user_links),
                    inline=False
                )
                
                # Add seating order to channel embed
                channel_embed.add_field(name="Seating Order", value=" -> ".join(seating_order), inline=False)
                
                # Update button states
                for item in self.children:
                    if isinstance(item, discord.ui.Button):
                        # Disable all buttons except cancel draft
                        if item.custom_id == f"cancel_draft_{self.draft_session_id}":
                            item.disabled = False
                        else:
                            item.disabled = True
                
                await db_session.commit()
        
        # Update the message with the new embed and disabled buttons
        await interaction.response.edit_message(embed=embed, view=self)
        
        # Send the channel announcement
        await interaction.channel.send(embed=channel_embed)

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
        
        # Mark as processing
        PROCESSING_ROOMS_PAIRINGS[session_id] = True
        
        try:
            await interaction.response.defer()
        except discord.errors.NotFound:
            print("Creating Rooms: Interaction not found or expired.")
            del PROCESSING_ROOMS_PAIRINGS[session_id]
            return
        
        try:
            # Disable the button
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.label == "Create Rooms & Post Pairings":
                    child.disabled = True
                    break

            # Use the common helper method
            result = await PersistentView.create_rooms_pairings(interaction.client, interaction.guild, session_id, interaction)
            
            # If rooms were already created, the method will return False and handle the message itself
            # Only need to update the UI when successful or for other failures
            if result:
                try:
                    await self.message.edit(view=self)
                except discord.errors.NotFound:
                    print(f"Cannot edit message - not found. Session ID: {session_id}")

        finally:
            # Clean up processing flag
            if session_id in PROCESSING_ROOMS_PAIRINGS:
                del PROCESSING_ROOMS_PAIRINGS[session_id]

    async def calculate_and_store_stakes(self, interaction: discord.Interaction, draft_session, cap_info=None):
        """Calculate and store stakes for a staked draft session."""
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                # Get all stake info records for this session
                stake_stmt = select(StakeInfo).where(StakeInfo.session_id == draft_session.session_id)
                results = await db_session.execute(stake_stmt)
                stake_info_records = results.scalars().all()
                
                # Build stakes dictionary
                stakes_dict = {record.player_id: record.max_stake for record in stake_info_records}
                
                # Build capping info dictionary - use passed cap_info if provided
                if cap_info is None:
                    # Fall back to using the is_capped values from stake_info_records if no cap_info provided
                    cap_info = {record.player_id: getattr(record, 'is_capped', True) for record in stake_info_records}
                
                # Get configuration
                from config import get_config
                config = get_config(interaction.guild_id)
                use_optimized = config.get("stakes", {}).get("use_optimized_algorithm", False)
                stake_multiple = config.get("stakes", {}).get("stake_multiple", 10)
                
                user_min_stake = draft_session.min_stake or 10
                
                # Use the router function with capping info
                stake_pairs = calculate_stakes_with_strategy(
                    draft_session.team_a, 
                    draft_session.team_b, 
                    stakes_dict,
                    min_stake=user_min_stake,  
                    multiple=stake_multiple,
                    use_optimized=use_optimized,
                    cap_info=cap_info
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

        # Only add admin roles to the Draft chat, not to team-specific channels
        if team_name == "Draft":
            # For the "Draft-chat" channel, add read permissions for admin role
            if admin_role:
                overwrites[admin_role] = discord.PermissionOverwrite(read_messages=True, manage_messages=True)
            
            # # For the combined "Draft-chat" channel, also give read access to anyone with the active role
            # if config["activity_tracking"]["enabled"]:
            #     active_role_name = config["activity_tracking"]["active_role"]
            #     active_role = discord.utils.get(guild.roles, name=active_role_name)
            #     if active_role:
            #         overwrites[active_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        # Add team members with read permission (this overrides any role-based permissions)
        for member in team_members:
            overwrites[member] = discord.PermissionOverwrite(read_messages=True, manage_messages=True)
        
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

    @classmethod
    async def create_rooms_pairings(cls, bot, guild, session_id, interaction=None, session_type=None):
        """Class method version of creating rooms and posting pairings"""
        logger.info("Starting create_rooms_pairings for session_id={}, session_type={}", session_id, session_type)
        try:
            async with AsyncSessionLocal() as db_session:
                async with db_session.begin():
                    logger.debug("Querying DraftSession from DB")
                    stmt = select(DraftSession).options(selectinload(DraftSession.match_results))\
                           .filter(DraftSession.session_id == session_id)
                    session = await db_session.scalar(stmt)

                    if not session:
                        logger.warning("Draft session not found for session_id={}", session_id)
                        if interaction:
                            await interaction.followup.send("Draft session not found.", ephemeral=True)
                        return False

                    if session.draft_chat_channel:
                        logger.info("Rooms already exist for session_id={}, channel={}", session_id, session.draft_chat_channel)
                        if interaction:
                            await interaction.followup.send(
                                "Rooms and pairings have already been created for this draft.", ephemeral=True)
                        return False

                    session.are_rooms_processing = True
                    session.session_stage = 'pairings'
                    logger.debug("Set session_stage to 'pairings' and are_rooms_processing=True")

                    # Calculate pairings
                    logger.debug("Calculating pairings for session_type={}", session.session_type)
                    if session.session_type != "swiss":
                        await calculate_pairings(session, db_session)
                    else:
                        state_to_save, match_counter = await calculate_pairings(session, db_session)
                        session.match_counter = match_counter
                        session.swiss_matches = state_to_save
                        logger.debug("Swiss pairings calculated: match_counter={}", match_counter)

                    # Update player stats
                    if session.session_type in ("random", "staked"):
                        logger.debug("Updating player stats for session_id={}", session_id)
                        await update_player_stats_for_draft(session.session_id, guild)

                    if session.session_type in ("random", "staked", "premade"):
                        logger.debug("Updating last draft timestamp for session_id={}", session_id)
                        await update_last_draft_timestamp(session.session_id, guild, bot)

                    # Prepare view for channel creation
                    temp_view = cls(bot, session_id, session_type or session.session_type)

                    # Create chat channels
                    draft_chat_channel = None
                    if session.session_type == "swiss":
                        sign_ups_list = list(session.sign_ups.keys())
                        logger.debug("Swiss sign-ups: {}", sign_ups_list)
                        all_members = []
                        for user_id in sign_ups_list:
                            member = guild.get_member(int(user_id))
                            if not member:
                                logger.warning("Member not found in guild for user_id={}", user_id)
                            else:
                                all_members.append(member)
                        channel = await temp_view.create_team_channel(guild, "Draft", all_members)
                        session.draft_chat_channel = str(channel)
                        draft_chat_channel = guild.get_channel(int(session.draft_chat_channel))
                        logger.info("Created swiss draft channel {}", session.draft_chat_channel)

                    elif session.session_type != "test":
                        logger.debug("Team A: {}, Team B: {}", session.team_a, session.team_b)
                        team_a_members, team_b_members = [], []
                        for user_id in session.team_a:
                            member = guild.get_member(int(user_id))
                            if member:
                                team_a_members.append(member)
                            else:
                                logger.warning("Team A member not found for user_id={}", user_id)
                        for user_id in session.team_b:
                            member = guild.get_member(int(user_id))
                            if member:
                                team_b_members.append(member)
                            else:
                                logger.warning("Team B member not found for user_id={}", user_id)

                        all_members = team_a_members + team_b_members
                        channel = await temp_view.create_team_channel(
                            guild, "Draft", all_members, session.team_a, session.team_b
                        )
                        session.draft_chat_channel = str(channel)
                        draft_chat_channel = guild.get_channel(int(session.draft_chat_channel))
                        logger.info("Created draft and team channels for session_id={}", session_id)
                        await temp_view.create_team_channel(guild, "Red-Team", team_a_members, session.team_a, session.team_b)
                        await temp_view.create_team_channel(guild, "Blue-Team", team_b_members, session.team_a, session.team_b)

                    else:
                        draft_chat_channel = guild.get_channel(int(session.draft_channel_id))
                        session.draft_chat_channel = session.draft_channel_id
                        logger.debug("Using test channel {}", session.draft_channel_id)

                    # Generate and send summary
                    draft_summary_embed = await generate_draft_summary_embed(bot, session.session_id)
                    sign_up_tags = ' '.join(f"<@{user_id}>" for user_id in session.sign_ups.keys())
                    auto_text = " (Auto-created)" if interaction is None else ""
                    logger.debug("Sending pairing announcement")
                    await draft_chat_channel.send(
                        f"Pairings posted below{auto_text}. Good luck in your matches! {sign_up_tags}"
                    )

                    if session.session_type == "staked":
                        stake_view = discord.ui.View(timeout=None)
                        stake_view.add_item(StakeCalculationButton(session.session_id))
                        draft_summary_message = await draft_chat_channel.send(embed=draft_summary_embed, view=stake_view)
                    else:
                        draft_summary_message = await draft_chat_channel.send(embed=draft_summary_embed)

                    if session.session_type != "test":
                        await draft_summary_message.pin()
                        logger.debug("Pinned draft summary message {}", draft_summary_message.id)
                    session.draft_summary_message_id = str(draft_summary_message.id)

                    # Delete original message
                    draft_channel_id = int(session.draft_channel_id)
                    original_message_id = int(session.message_id)
                    draft_channel = bot.get_channel(draft_channel_id)
                    if draft_channel:
                        try:
                            orig = await draft_channel.fetch_message(original_message_id)
                            await orig.delete()
                            logger.debug("Deleted original message {} in channel {}", original_message_id, draft_channel_id)
                        except discord.NotFound:
                            logger.warning("Original message {} not found in channel {}", original_message_id, draft_channel_id)
                        except discord.HTTPException as e:
                            logger.error("Failed to delete message {}: {}", original_message_id, e)

                    session.deletion_time = datetime.now() + timedelta(days=7)
                    logger.debug("Scheduled deletion time {}", session.deletion_time)
                    await db_session.commit()
                    logger.info("Database commit complete for session_id={}", session_id)

                # Post-commit
                logger.debug("Running post_pairings tasks")
                await post_pairings(bot, guild, session.session_id)
                from livedrafts import create_live_draft_summary
                await create_live_draft_summary(bot, session.session_id)

                if interaction:
                    await interaction.followup.send("Pairings posted.", ephemeral=True)
                    logger.debug("Sent confirmation to interaction")

                draft_setup_manager = DraftSetupManager.get_active_manager(session_id)
                if not draft_setup_manager:
                    draft_link = session.draft_link
                    guild_id = int(guild.id)
                    if draft_link:
                        logger.debug("Starting DraftLogManager for live session keep-alive")
                        from datacollections import DraftLogManager
                        manager = DraftLogManager(
                            session.session_id,
                            draft_link,
                            session.draft_id,
                            session.session_type,
                            session.cube,
                            discord_client=bot,
                            guild_id=guild_id
                        )
                        asyncio.create_task(manager.keep_draft_session_alive())
                    else:
                        logger.warning("Draft link not found for session_id={}", session_id)

                logger.info("create_rooms_pairings completed successfully for session_id={}", session_id)
                return True

        except Exception:
            logger.exception("Unhandled exception in create_rooms_pairings for session_id={}", session_id)
            if interaction:
                await interaction.followup.send("An error occurred.", ephemeral=True)
            return False

async def generate_ready_check_embed(ready_check_status, sign_ups, draft_link, draft_session=None):
    # Define a function to convert user IDs to their names using the sign_ups dictionary
    def get_names(user_ids):
        return "\n".join(sign_ups.get(user_id, "Unknown user") for user_id in user_ids) or "None"

    # Generate the embed with fields for "Ready", "Not Ready", and "No Response"
    embed = discord.Embed(title="Ready Check Initiated", description="Please indicate if you are ready.", color=discord.Color.gold())
    embed.add_field(name="Ready", value=get_names(ready_check_status['ready']), inline=False)
    embed.add_field(name="Not Ready", value=get_names(ready_check_status['not_ready']), inline=False)
    embed.add_field(name="No Response", value=get_names(ready_check_status['no_response']), inline=False)
    
    # Include personalized draft links for each user if draft_session is provided
    if draft_session:
        user_links = []
        for user_id, display_name in sign_ups.items():
            personalized_link = draft_session.get_draft_link_for_user(display_name)
            user_links.append(f"**{display_name}**: [Draft Link]({personalized_link})")
        
        # Use our helper function that safely splits fields if they're too long
        add_links_to_embed_safely(embed, user_links, "Your Personalized Draft Links")
    else:
        # Fallback if draft_session not provided (backwards compatibility)
        embed.add_field(name="Draftmancer Link", value=f"**➡️ [JOIN DRAFT HERE]({draft_link})⬅️**", inline=False)
    
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
        # Generate the updated embed with personalized links
        embed = await generate_ready_check_embed(session, draft_session.sign_ups, draft_session.draft_link, draft_session)

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
            
            # Record the leave event in history
            await SignUpHistory.record_signup_event(
                session_id=self.session_id,
                user_id=user_id_to_remove,
                display_name=removed_user_name,
                action="leave",
                guild_id=str(interaction.guild_id)
            )
            
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


async def update_last_draft_timestamp(session_id, guild, bot):
    """Update the last_draft_timestamp and assign Active role to all players in a draft"""
    guild_id = str(guild.id)
    current_time = datetime.now()
    
    # Get config for this guild to check role name
    from config import get_config
    config = get_config(guild_id)
    
    # Get activity tracking settings (for database update purposes only)
    activity_tracking_enabled = config.get("activity_tracking", {}).get("enabled", False)
    
    # Always get the active role name regardless of activity tracking setting
    active_role_name = config.get("activity_tracking", {}).get("active_role", "Active")
    
    # Find the active role if it exists
    active_role = None
    if active_role_name:
        active_role = discord.utils.get(guild.roles, name=active_role_name)
        if not active_role:
            logger.warning(f"Active role '{active_role_name}' not found in guild {guild.name}")
    
    async with AsyncSessionLocal() as db_session:
        async with db_session.begin():
            # Get the draft session
            stmt = select(DraftSession).where(DraftSession.session_id == session_id)
            draft_session = await db_session.scalar(stmt)
            
            if not draft_session:
                logger.error(f"Draft session {session_id} not found when updating timestamps.")
                return
            
            # Get all players in the draft
            player_ids = draft_session.team_a + draft_session.team_b
            
            # Update last_draft_timestamp for each player and assign Active role if it exists
            for player_id in player_ids:
                # Update timestamp in database
                from models.player import PlayerStats
                stmt = select(PlayerStats).where(
                    PlayerStats.player_id == player_id,
                    PlayerStats.guild_id == guild_id
                )
                player_stat = await db_session.scalar(stmt)
                
                if player_stat:
                    player_stat.last_draft_timestamp = current_time
                    logger.info(f"Updated last_draft_timestamp for player {player_stat.display_name}")
                else:
                    logger.warning(f"Player {player_id} not found in PlayerStats.")
                
                # Always assign Active role if it exists, regardless of activity tracking setting
                if active_role:
                    try:
                        # Get the member object
                        member = guild.get_member(int(player_id))
                        if member:
                            # Check if member already has the role
                            if active_role not in member.roles:
                                await member.add_roles(active_role)
                                logger.info(f"Added Active role to {member.display_name}")
                        else:
                            # Get username from draft session sign_ups if possible
                            username = draft_session.sign_ups.get(player_id, "Unknown")
                            logger.warning(f"Member {player_id} ({username}) not found in guild {guild.name}")
                    except Exception as e:
                        logger.error(f"Error assigning Active role to player {player_id}: {e}")
            
            await db_session.commit()


async def create_pairings_view(bot, guild, session_id, match_results):
    view = View(timeout=None)
    for match_result in match_results:
        
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
                    
                    if draft_session and (draft_session.session_type == "random" or draft_session.session_type == "staked"):
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

            # Get player names for match identification
            player1 = guild.get_member(int(match_result.player1_id))
            player2 = guild.get_member(int(match_result.player2_id))
            player1_name = player1.display_name if player1 else 'Unknown'
            player2_name = player2.display_name if player2 else 'Unknown'

            # Update the embed with new match results
            for match_result in match_results_for_this_message:
                if match_result.match_number == match_number:
                    # Determine which team won - default to black circle if no winner
                    winning_team_emoji = "⚫ "
                    if match_result.winner_id:
                        # Get draft session to check which team the winner belongs to
                        if match_result.winner_id in draft_session.team_a:
                            winning_team_emoji = "🔴 "  # Red emoji for Team A
                        elif match_result.winner_id in draft_session.team_b:
                            winning_team_emoji = "🔵 "  # Blue emoji for Team B
                    
                    # Update the field with the appropriate emoji
                    updated_value = f"{winning_team_emoji}**Match {match_result.match_number}**\n{player1_name}: {match_result.player1_wins} wins\n{player2_name}: {match_result.player2_wins} wins"
                    
                    # Use a more robust method to find the right field - match the match number and player names
                    found_match = False
                    for i, field in enumerate(embed.fields):
                        # Check if field has both match number and both players' names
                        if (f"Match {match_result.match_number}" in field.value and 
                            player1_name in field.value and 
                            player2_name in field.value):
                            embed.set_field_at(i, name=field.name, value=updated_value, inline=field.inline)
                            found_match = True
                            break
                    
                    if not found_match:
                        print(f"Could not find field for Match {match_result.match_number}")
                        # Try an alternative approach - just look for the match number
                        for i, field in enumerate(embed.fields):
                            if f"Match {match_result.match_number}" in field.value:
                                embed.set_field_at(i, name=field.name, value=updated_value, inline=field.inline)
                                found_match = True
                                break
            
            # Create a new view with updated button colors
            new_view = await self.create_updated_view_for_pairings_message(bot, guild.id, draft_session_id, pairing_message_id)

            try:
                # Edit the message with the updated embed and view
                await message.edit(embed=embed, view=new_view)
            except Exception as e:
                print(f"Error updating message: {e}")
                
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


class PersonalizedCapStatusView(discord.ui.View):
    def __init__(self, draft_session_id, user_id):
        super().__init__(timeout=None)
        self.draft_session_id = draft_session_id
        self.user_id = user_id
        
        # Add toggle button
        self.toggle_button = discord.ui.Button(
            label="Toggle Cap Status",
            style=discord.ButtonStyle.secondary,
            custom_id=f"toggle_bet_cap_{draft_session_id}"
        )
        self.toggle_button.callback = self.toggle_cap_callback
        self.add_item(self.toggle_button)
    
    async def toggle_cap_callback(self, interaction: discord.Interaction):
        user_id = self.user_id
        
        # Only the owner of the view should be able to toggle
        if str(interaction.user.id) != user_id:
            await interaction.response.send_message("This button is not for you.", ephemeral=True)
            return
            
        # Update stake info in database
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Get the user's stake info
                stake_stmt = select(StakeInfo).where(and_(
                    StakeInfo.session_id == self.draft_session_id,
                    StakeInfo.player_id == user_id
                ))
                stake_result = await session.execute(stake_stmt)
                stake_info = stake_result.scalars().first()
                
                if not stake_info:
                    await interaction.response.send_message("You need to set a stake amount first.", ephemeral=True)
                    return
                
                # Toggle the capping status
                current_cap_status = getattr(stake_info, 'is_capped', True)
                stake_info.is_capped = not current_cap_status
                
                # Update the database
                session.add(stake_info)
                await session.commit()
        
                # Create an updated view
                new_status = "ON 🧢" if stake_info.is_capped else "OFF 🏎️"
                style = discord.ButtonStyle.green if stake_info.is_capped else discord.ButtonStyle.red
                
                updated_view = discord.ui.View(timeout=None)
                status_button = discord.ui.Button(
                    label=f"Bet Cap: {new_status}",
                    style=style,
                    custom_id=f"bet_cap_status_{self.draft_session_id}",
                    disabled=True
                )
                updated_view.add_item(status_button)
                
                # Add the toggle button back
                toggle_button = discord.ui.Button(
                    label="Toggle Cap Status",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"toggle_bet_cap_{self.draft_session_id}"
                )
                toggle_button.callback = self.toggle_cap_callback
                updated_view.add_item(toggle_button)
                
                await interaction.response.edit_message(
                    content=f"Your bet cap status is now: {new_status}.\n" +
                    ("Your bet will be capped at the highest opponent bet." if stake_info.is_capped else 
                     "Your bet will NOT be capped by the opposing team's highest bet and may be spread across multiple opponents."),
                    view=updated_view
                )
                
                # Update the draft message
                await update_draft_message(interaction.client, self.draft_session_id)

async def show_personalized_cap_status(interaction, draft_session_id):
    """Shows a personalized cap status button for the user"""
    user_id = str(interaction.user.id)
    
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Get the user's stake info
            stake_stmt = select(StakeInfo).where(and_(
                StakeInfo.session_id == draft_session_id,
                StakeInfo.player_id == user_id
            ))
            stake_result = await session.execute(stake_stmt)
            stake_info = stake_result.scalars().first()
            
            if not stake_info:
                await interaction.response.send_message("You need to set a stake amount first.", ephemeral=True)
                return
            
            # Create the personalized view
            is_capped = getattr(stake_info, 'is_capped', True)
            status = "ON 🧢" if is_capped else "OFF 🏎️"
            style = discord.ButtonStyle.green if is_capped else discord.ButtonStyle.red
            
            view = discord.ui.View(timeout=None)
            status_button = discord.ui.Button(
                label=f"Bet Cap: {status}",
                style=style,
                custom_id=f"bet_cap_status_{draft_session_id}",
                disabled=True
            )
            view.add_item(status_button)
            
            # Add the toggle button 
            toggle_button = discord.ui.Button(
                label="Toggle Cap Status",
                style=discord.ButtonStyle.secondary,
                custom_id=f"toggle_bet_cap_{draft_session_id}"
            )
            
            # Define callback for the toggle button
            async def toggle_callback(interaction):
                await show_personalized_cap_status(interaction, draft_session_id)
            
            toggle_button.callback = toggle_callback
            view.add_item(toggle_button)
            
            await interaction.response.send_message(
                f"Your bet cap status is: {status}.\n" +
                ("Your bet will be capped at the highest opponent bet." if is_capped else 
                 "Your bet will NOT be capped by the opposing team's highest bet and may be spread across multiple opponents."),
                view=view,
                ephemeral=True
            )
            
                                    
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
        
        # Ensure sign_ups is not None before accessing its length
        if draft_session.sign_ups is None:
            draft_session.sign_ups = {}
            # Update the session in the database with the initialized sign_ups
            async with AsyncSessionLocal() as db_session:
                async with db_session.begin():
                    await db_session.execute(
                        update(DraftSession)
                        .where(DraftSession.session_id == draft_session.session_id)
                        .values(sign_ups={})
                    )
                    await db_session.commit()
            logger.info(f"Initialized empty sign_ups for session ID: {session_id}")
            
        sign_up_count = len(draft_session.sign_ups)
        sign_ups_field_name = "Sign-Ups:"
        
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
                        stake_amount = stake_info.max_stake
                        is_capped = getattr(stake_info, 'is_capped', True)  # Default to True if not set
                        stake_info_by_player[stake_info.player_id] = {
                            'amount': stake_amount,
                            'is_capped': is_capped
                        }
        
        # Create sign-ups string with stake amounts for staked drafts
        if draft_session.session_type == "staked":
            sign_ups_list = []
            for user_id, display_name in draft_session.sign_ups.items():
                # Create user-specific draft link
                user_draft_link = draft_session.get_draft_link_for_user(display_name)
                # Create hyperlink markdown format
                linked_name = f"[{display_name}]({user_draft_link})"
                # Default to "Not set" if no stake has been set yet
                if user_id in stake_info_by_player:
                    stake_amount = stake_info_by_player[user_id]['amount']
                    is_capped = stake_info_by_player[user_id]['is_capped']
                    capped_emoji = "🧢" if is_capped else "🏎️"  # Cap emoji for capped, lightning for uncapped
                    sign_ups_list.append((user_id, linked_name, stake_amount, is_capped, capped_emoji))
                else:
                    sign_ups_list.append((user_id, linked_name, "Not set", True, "❓"))
            
            # Sort by stake amount (highest first)
            # Convert "Not set" to -1 for sorting purposes
            def sort_key(item):
                stake = item[2]
                return -1 if stake == "Not set" else stake
            
            sign_ups_list.sort(key=sort_key, reverse=True)
            
            # Format with stakes and capping status
            formatted_sign_ups = []
            for user_id, display_name, stake_amount, is_capped, emoji in sign_ups_list:
                if stake_amount == "Not set":
                    formatted_sign_ups.append(f"❌ Not set: {display_name}")
                else:
                    formatted_sign_ups.append(f"{emoji} {stake_amount} tix: {display_name}")
            
            sign_ups_str = f"**Players ({sign_up_count}):**\n" + ('\n'.join(formatted_sign_ups) if formatted_sign_ups else 'No players yet.')
        else:
            if draft_session.sign_ups:
                linked_names = []
                for user_id, display_name in draft_session.sign_ups.items():
                    user_draft_link = draft_session.get_draft_link_for_user(display_name)
                    linked_name = f"[{display_name}]({user_draft_link})"
                    linked_names.append(linked_name)
                sign_ups_str = f"**Players ({sign_up_count}):**\n" + '\n'.join(linked_names)
            else:
                sign_ups_str = f"**Players (0):**\nNo players yet."
        
        # Helper function to update or add fields consistently
        def update_field(field_name, field_value, inline=False, expected_index=None):
            field_index = None
            # Look for the field by name
            for i, field in enumerate(embed.fields):
                if field.name == field_name:
                    field_index = i
                    break
            
            # If field exists, update it
            if field_index is not None:
                embed.set_field_at(field_index, name=field_name, value=field_value, inline=inline)
                logger.info(f"Updated {field_name} field for session {session_id}")
            else:
                # Field doesn't exist, add it
                logger.warning(f"{field_name} field not found in embed for session {session_id}, adding it")
                embed.add_field(name=field_name, value=field_value, inline=inline)
        
        # Find and remove any existing sign-up continuation fields to start fresh
        fields_to_remove = []
        for i, field in enumerate(embed.fields):
            if field.name.startswith(sign_ups_field_name) and field.name != sign_ups_field_name:
                fields_to_remove.append(i)
        
        # Remove fields in reverse order
        for idx in sorted(fields_to_remove, reverse=True):
            embed.remove_field(idx)
        
        # Check if the sign-ups string is too long
        if len(sign_ups_str) > 1000:  # Using 1000 to be safe (Discord limit is 1024)
            # Split the sign-ups into parts using our helper function
            parts = split_content_for_embed(sign_ups_str, include_header=True)
            
            # Update or add fields with standardized names
            for i, part in enumerate(parts):
                field_name = sign_ups_field_name if i == 0 else f"{sign_ups_field_name} (cont. {i})"
                update_field(field_name, part, inline=False)
        else:
            # Use the original approach for short sign-ups lists
            update_field(sign_ups_field_name, sign_ups_str, inline=False)
        
        # Update cube field
        cube_field_name = "Cube:"
        cube_field_value = f"[{draft_session.cube}](https://cubecobra.com/cube/list/{draft_session.cube})"
        update_field(cube_field_name, cube_field_value, inline=True)
                
        # Get the thumbnail URL for the cube
        thumbnail_url = get_cube_thumbnail_url(draft_session.cube)
        embed.set_thumbnail(url=thumbnail_url)
        logger.info(f"Updated thumbnail for cube: {draft_session.cube}")
        
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
        from services.draft_setup_manager import DraftSetupManager, ACTIVE_MANAGERS
        
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
        
        # Check if there's an active draft manager for this session
        manager = DraftSetupManager.get_active_manager(self.draft_session_id)
        if manager:
            logger.info(f"Found active draft manager for session {self.draft_session_id}, marking as cancelled")
            # Mark the draft as cancelled to prevent log collection
            await manager.mark_draft_cancelled()
            
            # Disconnect the manager from Draftmancer
            logger.info(f"Disconnecting draft manager for session {self.draft_session_id} from Draftmancer")
            await manager.disconnect_safely()
            
            # Verify manager is removed from active managers registry
            if self.draft_session_id not in ACTIVE_MANAGERS:
                logger.success(f"Successfully removed manager for session {self.draft_session_id} from active managers")
            else:
                logger.warning(f"Failed to remove manager for session {self.draft_session_id} from active managers registry")
        else:
            logger.info(f"No active draft manager found for session {self.draft_session_id}")
        
        # Then delete the message
        if channel:
            try:
                message = await channel.fetch_message(int(session.message_id))
                await message.delete()
            except Exception as e:
                logger.error(f"Failed to delete draft message: {e}")
        
        # Remove from database
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                await db_session.delete(session)
                await db_session.commit()
                logger.info(f"Removed draft session {self.draft_session_id} from database")

        # # Cancel any scheduled auto-pairings task
        # if self.draft_session_id in PersistentView.AUTO_PAIRINGS_TASKS:
        #     PersistentView.AUTO_PAIRINGS_TASKS[self.draft_session_id].cancel()
        #     del PersistentView.AUTO_PAIRINGS_TASKS[self.draft_session_id]

        await interaction.followup.send("The draft has been canceled.", ephemeral=True)

    @discord.ui.button(label="No, Keep Draft", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        # Disable buttons
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Draft cancellation aborted.", view=self)


class StakeOptionsSelect(discord.ui.Select):
    def __init__(self, draft_session_id, draft_link, user_display_name, min_stake, has_draftmancer_role=False):
        self.draft_session_id = draft_session_id
        self.draft_link = draft_link
        self.user_display_name = user_display_name
        self.min_stake = min_stake
        self.has_draftmancer_role = has_draftmancer_role

        options = []
        if self.min_stake <= 10:
            options.append(discord.SelectOption(label="10 TIX", value="10"))
        if self.min_stake <= 20:
            options.append(discord.SelectOption(label="20 TIX", value="20"))
        if self.min_stake <= 50:   
            options.append(discord.SelectOption(label="50 TIX", value="50"))
        if self.min_stake <= 100:   
            options.append(discord.SelectOption(label="100 TIX", value="100"))
        options.append(discord.SelectOption(label="Over 100 TIX", value="over_100"))

        super().__init__(placeholder=f"Select your maximum bet... ", min_values=1, max_values=1, options=options)
        
    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)
        
        # Load the user's saved preference
        from preference_service import get_player_bet_capping_preference
        is_capped = await get_player_bet_capping_preference(user_id, guild_id)
        
        selected_value = self.values[0]
        
        if selected_value == "over_100":
            # Create modal for custom amount over 100
            stake_modal = StakeModal(over_100=True)
            stake_modal.draft_session_id = self.draft_session_id
            stake_modal.draft_link = self.draft_link
            stake_modal.user_display_name = self.user_display_name
            stake_modal.has_draftmancer_role = self.has_draftmancer_role
            
            # IMPORTANT: Set the default value based on saved preference before showing the modal
            stake_modal.default_cap_setting = is_capped
            # Update the cap checkbox value based on the preference
            stake_modal.cap_checkbox.value = "yes" if is_capped else "no"
            
            await interaction.response.send_modal(stake_modal)
        else:
            # Process the selected preset stake amount with the saved preference
            stake_amount = int(selected_value)
            
            # Add user to sign_ups and handle stake submission using saved preference
            await self.handle_stake_submission(interaction, stake_amount, is_capped=is_capped)
            
    async def handle_stake_submission(self, interaction, stake_amount, is_capped=True):
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)
        display_name = str(interaction.user.display_name)
        
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Get the draft session
                draft_stmt = select(DraftSession).where(DraftSession.session_id == self.draft_session_id)
                draft_result = await session.execute(draft_stmt)
                draft_session = draft_result.scalars().first()
                
                if not draft_session:
                    await interaction.response.send_message("Draft session not found.", ephemeral=True)
                    return
                
                # Update sign_ups
                sign_ups = draft_session.sign_ups or {}
                sign_ups[user_id] = interaction.user.display_name
                
                # Record the signup event in history
                await SignUpHistory.record_signup_event(
                    session_id=self.draft_session_id,
                    user_id=user_id,
                    display_name=display_name,
                    action="join",
                    guild_id=str(interaction.guild_id)
                )
                
                # Update draftmancer_role_users if user has the role
                draftmancer_role_users = draft_session.draftmancer_role_users or []
                if self.has_draftmancer_role and display_name not in draftmancer_role_users:
                    draftmancer_role_users.append(display_name)
                
                # Check if this is the 5th person to sign up AND we haven't pinged yet
                should_ping = False
                now = datetime.now()
                ping_cooldown = draft_session.draft_start_time + timedelta(minutes=30)
                
                if len(sign_ups) in (5, 7) and not draft_session.should_ping and now > ping_cooldown:
                    should_ping = True
                
                # Update draft session with sign_ups, draftmancer_role_users, and should_ping flag if needed
                values_to_update = {
                    "sign_ups": sign_ups,
                    "draftmancer_role_users": draftmancer_role_users
                }
                if should_ping:
                    values_to_update["should_ping"] = True

                # Reset the inactivity timer when a user signs up (if still in initial queue)
                # Skip resetting deletion_time for guild ID 1229863996929216686
                if not draft_session.session_stage and draft_session.guild_id != "1229863996929216686":
                    values_to_update["deletion_time"] = datetime.now() + timedelta(minutes=180)

                await session.execute(
                    update(DraftSession).
                    where(DraftSession.session_id == self.draft_session_id).
                    values(**values_to_update)
                )
                
                # Check if a stake record already exists for this player
                stake_stmt = select(StakeInfo).where(and_(
                    StakeInfo.session_id == self.draft_session_id,
                    StakeInfo.player_id == user_id
                ))
                stake_result = await session.execute(stake_stmt)
                stake_info = stake_result.scalars().first()
                
                if stake_info:
                    # Update existing stake
                    stake_info.max_stake = stake_amount
                    stake_info.is_capped = is_capped
                else:
                    # Create new stake record
                    stake_info = StakeInfo(
                        session_id=self.draft_session_id,
                        player_id=user_id,
                        max_stake=stake_amount,
                        is_capped=is_capped
                    )
                    session.add(stake_info)
                
                await session.commit()
        
        # Re-fetch the draft session after commit to get the latest data
        async with AsyncSessionLocal() as session:
            draft_stmt = select(DraftSession).where(DraftSession.session_id == self.draft_session_id)
            draft_result = await session.execute(draft_stmt)
            draft_session_updated = draft_result.scalars().first()
            
            if not draft_session_updated:
                print("Failed to fetch updated draft session after stake selection.")
                return
            
            # Send ping if needed (5th player and haven't pinged yet)
            if should_ping:
                # Get the drafter role from the config
                from config import get_config
                guild_config = get_config(interaction.guild_id)
                drafter_role_name = guild_config["roles"]["drafter"]
                
                # Find the role in the guild
                guild = interaction.guild
                drafter_role = discord.utils.get(guild.roles, name=drafter_role_name)
                
                if drafter_role:
                    # Get the channel where the draft message is
                    channel = await interaction.client.fetch_channel(draft_session_updated.draft_channel_id)
                    if channel:
                        player_count = len(sign_ups)
                        await channel.send(f"{player_count} Players in queue! {drafter_role.mention}")
        
        # Confirm stake and provide draft link
        cap_status = "capped at the highest opponent bet" if is_capped else "NOT capped (full action)"
        signup_message = f"You've set your maximum stake to {stake_amount} tix."
        signup_message += f"\nYour bet will be {cap_status}."
            
        if self.draft_link:
            display_name = interaction.user.display_name
            personalized_link = draft_session_updated.get_draft_link_for_user(display_name)
            signup_message += f"\n\nYou are now signed up. Join Here: {personalized_link}"
        
        # Send confirmation message
        await interaction.response.send_message(signup_message, ephemeral=True)
        
        # Update the draft message to reflect the new list of sign-ups
        await update_draft_message(interaction.client, self.draft_session_id)


class StakeOptionsView(discord.ui.View):
    def __init__(self, draft_session_id, draft_link, user_display_name, min_stake, has_draftmancer_role=False):
        super().__init__(timeout=300)  # 5 minute timeout
        self.add_item(StakeOptionsSelect(
            draft_session_id, 
            draft_link, 
            user_display_name, 
            min_stake,
            has_draftmancer_role
        ))
        
class StakeModal(discord.ui.Modal):
    def __init__(self, over_100=False):
        super().__init__(title="Enter Maximum Bet")
        
        self.over_100 = over_100
        self.default_cap_setting = True  
        self.has_draftmancer_role = False  
        placeholder_text = "Reminder: Your bet can fill multiple bets when possible" if over_100 else "Enter maximum amount you're willing to bet"
        
        self.stake_input = discord.ui.InputText(
            label="Enter max bet (increments of 50)",
            placeholder=placeholder_text,
            required=True
        )
        self.add_item(self.stake_input)
        
        # Add checkbox for bet capping (only visible for over_100)
        if over_100:
            self.cap_checkbox = discord.ui.InputText(
                label="Cap my bet at highest opponent bet",
                placeholder="Type 'yes' to cap or 'no' to keep your full bet",
                required=True,
                value="yes"  # Will be updated with default_cap_setting before showing
            )
            self.add_item(self.cap_checkbox)
        
        # These will be set separately before sending the modal
        self.draft_session_id = None
        self.draft_link = None
        self.user_display_name = None

    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)
        
        try:
            # Parse the stake amount
            max_stake = int(self.stake_input.value)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)
            return
        
        # Determine if bet should be capped
        is_capped = True  # Default for regular stakes
        if self.over_100:
            cap_value = self.cap_checkbox.value.lower()
            is_capped = cap_value in ('yes', 'y', 'true')
        
        # Validation for over 100 stakes
        if self.over_100:
            if max_stake <= 100:
                await interaction.response.send_message("Amount must be greater than 100 tix.", ephemeral=True)
                return
            if max_stake % 50 != 0:
                await interaction.response.send_message("Amount must be a multiple of 50 (e.g., 150, 200, 250).", ephemeral=True)
                return
        
        try:
            # Update the player's preference in the database for future drafts
            from preference_service import update_player_bet_capping_preference
            await update_player_bet_capping_preference(user_id, guild_id, is_capped)
            
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    # Check if draft_session_id is set properly
                    if not self.draft_session_id:
                        await interaction.response.send_message("Error: Draft session ID is missing. Please try again.", ephemeral=True)
                        return
                    
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
                    
                    # Only add to sign_ups if stake is valid
                    sign_ups = draft_session.sign_ups or {}
                    display_name = self.user_display_name or interaction.user.display_name
                    sign_ups[user_id] = display_name
                    
                    # Update draftmancer_role_users if user has the role
                    draftmancer_role_users = draft_session.draftmancer_role_users or []
                    if self.has_draftmancer_role and display_name not in draftmancer_role_users:
                        draftmancer_role_users.append(display_name)
                    
                    # Update the draft session with the new sign_ups and draftmancer_role_users
                    await session.execute(
                        update(DraftSession).
                        where(DraftSession.session_id == self.draft_session_id).
                        values(
                            sign_ups=sign_ups,
                            draftmancer_role_users=draftmancer_role_users
                        )
                    )
                    
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
                        stake_info.is_capped = is_capped
                    else:
                        # Create new stake record
                        stake_info = StakeInfo(
                            session_id=self.draft_session_id,
                            player_id=user_id,
                            max_stake=max_stake,
                            is_capped=is_capped
                        )
                        session.add(stake_info)
                    
                    await session.commit()
            
            # Create a response that includes the stake confirmation, reminder about stake usage, and draft link
            cap_status = "capped at the highest opponent bet" if is_capped else "NOT capped (full action)"
            signup_message = f"You've set your maximum stake to {max_stake} tix."
            signup_message += f"\nYour bet will be {cap_status}."
            
            # Add note about preference being saved for future drafts
            signup_message += f"\n\nThis setting will be remembered for future drafts."
            
            # Add reminder for stakes over 100
            if max_stake > 100:
                signup_message += "\n\nReminder: Your max bet will be used to fill as many opposing team bets as possible."
                
            if self.draft_link:
                draft_link = draft_session.get_draft_link_for_user(interaction.user.display_name)
                signup_message += f"\n\nYou are now signed up. Join Here: {draft_link}"
            
            # Send the confirmation
            await interaction.response.send_message(signup_message, ephemeral=True)
            
            # Update the draft message to reflect the new list of sign-ups
            await update_draft_message(interaction.client, self.draft_session_id)
            
        except Exception as e:
            # Add detailed error handling to help diagnose the issue
            error_message = f"An error occurred: {str(e)}"
            print(f"StakeModal callback error: {error_message}")  # Log to console
            try:
                await interaction.response.send_message(error_message, ephemeral=True)
            except:
                # If interaction has already been responded to, try followup
                try:
                    await interaction.followup.send(error_message, ephemeral=True)
                except Exception as followup_error:
                    print(f"Failed to send error message to user: {followup_error}")

class PaginatedStakeExplanation(discord.ui.View):
    def __init__(self, embeds, timeout=180):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.current_page = 0
        self.update_button_states()
        
    def update_button_states(self):
        # Disable previous button if we're on the first page
        self.children[0].disabled = (self.current_page == 0)
        # Disable next button if we're on the last page
        self.children[2].disabled = (self.current_page == len(self.embeds) - 1)
        # Update page counter
        self.children[1].label = f"Page {self.current_page + 1}/{len(self.embeds)}"
    
    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_button(self, button, interaction):
        # Py-cord has button first, then interaction
        if self.current_page > 0:
            self.current_page -= 1
            self.update_button_states()
            await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)
    
    @discord.ui.button(label="Page 1/1", style=discord.ButtonStyle.gray, disabled=True)
    async def page_counter(self, button, interaction):
        # This button just shows the current page and is not meant to be clicked
        await interaction.response.defer()
    
    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_button(self, button, interaction):
        # Py-cord has button first, then interaction
        if self.current_page < len(self.embeds) - 1:
            self.current_page += 1
            self.update_button_states()
            await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

class StakeCalculationButton(discord.ui.Button):
    def __init__(self, session_id):
        super().__init__(
            label="How Bets Were Calculated",
            style=discord.ButtonStyle.green,
            custom_id=f"stake_calculation_{session_id}"
        )
        self.session_id = session_id
        
    async def callback(self, interaction: discord.Interaction):
        """Show a detailed explanation of how stakes were calculated"""
        await interaction.response.defer(ephemeral=True)
        
        # Fetch the draft session
        draft_session = await get_draft_session(self.session_id)
        if not draft_session:
            await interaction.followup.send("Draft session not found.", ephemeral=True)
            return
        
        try:
            # Create player ID to name mapping
            player_names = {player_id: draft_session.sign_ups.get(player_id, "Unknown") 
                        for player_id in list(draft_session.team_a) + list(draft_session.team_b)}
            
            # Create mapping of player IDs to stakes and cap preferences
            max_stakes = {}
            cap_info = {}
            
            # Fetch the stake info records
            async with AsyncSessionLocal() as session:
                stake_stmt = select(StakeInfo).where(StakeInfo.session_id == self.session_id)
                results = await session.execute(stake_stmt)
                stake_infos = results.scalars().all()
                
                for info in stake_infos:
                    max_stakes[info.player_id] = info.max_stake
                    cap_info[info.player_id] = info.is_capped  
                
                # Fetch actual stake pairs that were created
                stake_lines, total_stakes = await get_formatted_stake_pairs(
                    self.session_id,
                    draft_session.sign_ups
                )
            
            # Store the original stakes before any capping
            original_stakes = {player_id: stake for player_id, stake in max_stakes.items()}
            
            # Get the highest stake for each team
            max_stake_a = max([original_stakes.get(player_id, 0) for player_id in draft_session.team_a]) if draft_session.team_a else 0
            max_stake_b = max([original_stakes.get(player_id, 0) for player_id in draft_session.team_b]) if draft_session.team_b else 0
            
            # Create capped_stakes dict and track players who were capped
            capped_stakes = {player_id: stake for player_id, stake in original_stakes.items()}
            capped_players = []  # List of (player_id, original_stake, capped_stake)
            
            # Apply bet capping for Team A
            for player_id in draft_session.team_a:
                if player_id in cap_info and cap_info[player_id] and original_stakes.get(player_id, 0) > max_stake_b:
                    capped_players.append((player_id, original_stakes[player_id], max_stake_b))
                    capped_stakes[player_id] = max_stake_b
            
            # Apply bet capping for Team B
            for player_id in draft_session.team_b:
                if player_id in cap_info and cap_info[player_id] and original_stakes.get(player_id, 0) > max_stake_a:
                    capped_players.append((player_id, original_stakes[player_id], max_stake_a))
                    capped_stakes[player_id] = max_stake_a
            
            # Extract player IDs and their final allocations from stake pairs
            final_allocations = {}
            
            # Parse the stake lines to build final_allocations
            for line in stake_lines:
                # Parse lines like "DevUser3 vs User1: 20 tix"
                parts = line.split(":")
                if len(parts) == 2:
                    players_part = parts[0].strip()
                    amount_part = parts[1].strip()
                    
                    # Extract players
                    players = players_part.split(" vs ")
                    if len(players) == 2:
                        player_a_name = players[0].strip()
                        player_b_name = players[1].strip()
                        
                        # Extract amount
                        amount = int(amount_part.split(" ")[0])
                        
                        # Find player IDs matching these names
                        player_a_id = None
                        player_b_id = None
                        
                        for pid, name in player_names.items():
                            if name == player_a_name:
                                player_a_id = pid
                            elif name == player_b_name:
                                player_b_id = pid
                        
                        if player_a_id and player_b_id:
                            # Add to final allocations
                            if player_a_id not in final_allocations:
                                final_allocations[player_a_id] = 0
                            if player_b_id not in final_allocations:
                                final_allocations[player_b_id] = 0
                            
                            final_allocations[player_a_id] += amount
                            final_allocations[player_b_id] += amount
            
            # Calculate team totals AFTER capping
            team_a_total = sum(capped_stakes.get(player_id, 0) for player_id in draft_session.team_a)
            team_b_total = sum(capped_stakes.get(player_id, 0) for player_id in draft_session.team_b)
            
            # Determine min team and max team based on capped totals
            if team_a_total <= team_b_total:
                min_team = draft_session.team_a
                max_team = draft_session.team_b
                min_team_name = "Team A (Red)"
                max_team_name = "Team B (Blue)"
                min_team_total = team_a_total
                max_team_total = team_b_total
            else:
                min_team = draft_session.team_b
                max_team = draft_session.team_a
                min_team_name = "Team B (Blue)"
                max_team_name = "Team A (Red)"
                min_team_total = team_b_total
                max_team_total = team_a_total
            
            # Calculate minimum required capacity for each team
            min_team_min_required = 0
            for player_id in min_team:
                stake = capped_stakes.get(player_id, 0)
                if stake <= 50:
                    min_team_min_required += stake
                else:
                    min_team_min_required += 50
            
            max_team_min_required = 0
            for player_id in max_team:
                stake = capped_stakes.get(player_id, 0)
                if stake <= 50:
                    max_team_min_required += stake
                else:
                    max_team_min_required += 50
            
            # Determine which method was actually used based on log analysis
            tiered_method_used = not (min_team_total < max_team_min_required)
            
            # Create the explanation embeds
            embeds = await self.generate_explanation(
                draft_session, min_team, max_team, min_team_name, max_team_name,
                min_team_total, max_team_total, capped_stakes, original_stakes,
                player_names, min_team_min_required, max_team_min_required,
                cap_info, capped_players, tiered_method_used, stake_lines,
                final_allocations
            )
            
            # Create the paginated view
            view = PaginatedStakeExplanation(embeds)
            
            # Send the first embed with the view
            await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(f"Error generating explanation: {str(e)}", ephemeral=True)
            import traceback
            traceback.print_exc()
    
    async def generate_explanation(self, draft_session, min_team, max_team, 
                            min_team_name, max_team_name, min_team_total, 
                            max_team_total, capped_stakes, original_stakes,
                            player_names, min_team_min_required, max_team_min_required, 
                            cap_info, capped_players, tiered_method_used, stake_lines,
                            final_allocations):
        """Generate a series of explanation embeds with the ORIGINAL content order"""
        
        # Method name for titles
        method_name = "Tiered" if tiered_method_used else "Proportional"
        
        # Create the embeds list
        embeds = []
        
        # PAGE 1: Core Principles and Step 0: Apply Bet Cap
        embed1 = discord.Embed(
            title=f"Dynamic Bet System: {method_name} Approach (1/3)",
            color=discord.Color.green() if tiered_method_used else discord.Color.gold()
        )
        
        # Core Principles section - SAME AS ORIGINAL
        embed1.add_field(
            name="Core Principles",
            value=(
                "• Players never bet more than their maximum specified amount\n"
                "• Teams were created randomly FIRST, then bets were allocated"
            ),
            inline=False
        )
        
        # Bet Capping Section - SAME AS ORIGINAL
        max_stake_a = max([original_stakes.get(player_id, 0) for player_id in draft_session.team_a]) if draft_session.team_a else 0
        max_stake_b = max([original_stakes.get(player_id, 0) for player_id in draft_session.team_b]) if draft_session.team_b else 0
        
        # Create a set of capped player IDs for quick lookup
        capped_player_ids = {player_id for player_id, _, _ in capped_players}
        
        # Team A capping info
        team_a_capping = []
        team_a_capping.append(f"Team B max bet: {max_stake_b} tix")
        
        for player_id in draft_session.team_a:
            if player_id in original_stakes:
                player_name = player_names.get(player_id, "Unknown")
                original_stake = original_stakes.get(player_id, 0)
                
                # Use cap_info to determine emoji
                if player_id in cap_info and cap_info[player_id]:
                    # Player opted for capping - use cap emoji
                    if player_id in capped_player_ids:
                        # This player was actually capped
                        capped_stake = capped_stakes.get(player_id, 0)
                        team_a_capping.append(f"🧢 {player_name}: {original_stake} → {capped_stake} tix")
                    else:
                        # Player opted for capping but wasn't capped
                        team_a_capping.append(f"🧢 {player_name}: {original_stake} tix")
                else:
                    # Player opted for uncapped
                    team_a_capping.append(f"🏎️ {player_name}: {original_stake} tix")
        
        # Team B capping info
        team_b_capping = []
        team_b_capping.append(f"Team A max bet: {max_stake_a} tix")
        
        for player_id in draft_session.team_b:
            if player_id in original_stakes:
                player_name = player_names.get(player_id, "Unknown")
                original_stake = original_stakes.get(player_id, 0)
                
                # Use cap_info to determine emoji
                if player_id in cap_info and cap_info[player_id]:
                    # Player opted for capping - use cap emoji
                    if player_id in capped_player_ids:
                        # This player was actually capped
                        capped_stake = capped_stakes.get(player_id, 0)
                        team_b_capping.append(f"🧢 {player_name}: {original_stake} → {capped_stake} tix")
                    else:
                        # Player opted for capping but wasn't capped
                        team_b_capping.append(f"🧢 {player_name}: {original_stake} tix")
                else:
                    # Player opted for uncapped
                    team_b_capping.append(f"🏎️ {player_name}: {original_stake} tix")
        
        # Add the bet capping section to the embed - EXACTLY AS ORIGINAL
        embed1.add_field(
            name="Step 0: Apply Bet Cap",
            value=(
                "**Team Red**\n" + 
                "\n".join(team_a_capping) +
                "\n\n**Team Blue**\n" + 
                "\n".join(team_b_capping)
            ),
            inline=False
        )
        
        embeds.append(embed1)
        
        # PAGE 2: Determine Min and Max Teams and Method Selection
        embed2 = discord.Embed(
            title=f"Dynamic Bet System: {method_name} Approach (2/3)",
            color=discord.Color.green() if tiered_method_used else discord.Color.gold()
        )
        
        # Initial Team Analysis - SAME AS ORIGINAL
        min_team_stakes = []
        for player_id in min_team:
            if player_id in capped_stakes:
                player_name = player_names.get(player_id, "Unknown")
                stake = capped_stakes.get(player_id, 0)
                min_team_stakes.append(f"{player_name}: {stake} tix")
        
        max_team_stakes = []
        for player_id in max_team:
            if player_id in capped_stakes:
                player_name = player_names.get(player_id, "Unknown")
                stake = capped_stakes.get(player_id, 0)
                max_team_stakes.append(f"{player_name}: {stake} tix")
        
        embed2.add_field(
            name="Determine Min and Max Teams",
            value=(
                f"**{min_team_name}** (Min Team - Total: {min_team_total} tix):\n" + 
                "\n".join(min_team_stakes) + 
                f"\n\n**{max_team_name}** (Max Team - Total: {max_team_total} tix):\n" + 
                "\n".join(max_team_stakes)
            ),
            inline=False
        )
        
        # Method Selection - SAME AS ORIGINAL
        embed2.add_field(
            name=f"**Method Used: {method_name} Approach**",
            value=(
                ("Each team had sufficient capacity to meet minimum requirements, so the tiered approach was used." 
                if tiered_method_used else 
                f"One or both teams couldn't meet minimum requirements: {min_team_name} capacity ({min_team_total} tix) "
                f"vs {max_team_name} min required ({max_team_min_required} tix), so the proportional approach was used.")
            ),
            inline=False
        )
        
        embeds.append(embed2)
        
        # PAGE 3: Allocation Phase and Bet Matching Phase
        embed3 = discord.Embed(
            title=f"Dynamic Bet System: {method_name} Approach (3/3)",
            color=discord.Color.green() if tiered_method_used else discord.Color.gold()
        )
        
        # Allocation Phase - EXACTLY AS ORIGINAL
        allocation_text = []
        
        # Group max team players by tier
        max_team_low_tier = [(p, capped_stakes.get(p, 0)) for p in max_team 
                        if p in capped_stakes and capped_stakes.get(p, 0) <= 50]
        max_team_high_tier = [(p, capped_stakes.get(p, 0)) for p in max_team 
                            if p in capped_stakes and capped_stakes.get(p, 0) > 50]
        
        if tiered_method_used:
            # Step 1: 100% allocations to min team and low tier max team
            allocation_text.append("**Step 1: Allocate 100% of max bets to Min Team and Max Team ≤50 tix players**")
            
            # Min team allocations
            allocation_text.append(f"*{min_team_name} (Min Team):*")
            for player_id in min_team:
                if player_id in capped_stakes:
                    player_name = player_names.get(player_id, "Unknown")
                    stake = capped_stakes.get(player_id, 0)
                    allocated = final_allocations.get(player_id, 0)
                    percentage = (allocated / stake * 100) if stake > 0 else 0
                    allocation_text.append(f"{player_name}: {allocated}/{stake} tix ({percentage:.1f}%)")
            
            # Max team low tier allocations
            if max_team_low_tier:
                allocation_text.append(f"\n*{max_team_name} (Max Team ≤50 tix):*")
                low_tier_total = 0
                for player_id, stake in max_team_low_tier:
                    player_name = player_names.get(player_id, "Unknown")
                    allocated = final_allocations.get(player_id, 0) 
                    percentage = (allocated / stake * 100) if stake > 0 else 0
                    allocation_text.append(f"{player_name}: {allocated}/{stake} tix ({percentage:.1f}%)")
                    low_tier_total += stake
            
            # Step 2: Calculate remaining capacity for high tier
            if max_team_high_tier:
                allocation_text.append("\n**Step 2: Calculate Remaining Capacity for High Tier Players**")
                
                remaining_capacity = min_team_total - low_tier_total
                high_tier_total = sum(stake for _, stake in max_team_high_tier)
                
                allocation_text.append(f"Min Team Total: {min_team_total} tix")
                allocation_text.append(f"Low Tier Max Team Total: {low_tier_total} tix")
                allocation_text.append(f"Remaining Capacity: {min_team_total} - {low_tier_total} = {remaining_capacity} tix")
                allocation_text.append(f"High Tier Max Team Total: {high_tier_total} tix")
                
                percentage = (remaining_capacity / high_tier_total * 100) if high_tier_total > 0 else 0
                allocation_text.append(f"Allocation Percentage: {percentage:.1f}%")
                # High tier allocations
                allocation_text.append(f"*{max_team_name} (Max Team >50 tix):*")
                for player_id, stake in max_team_high_tier:
                    player_name = player_names.get(player_id, "Unknown")
                    allocated = final_allocations.get(player_id, 0)
                    percentage = (allocated / stake * 100) if stake > 0 else 0
                    allocation_text.append(f"{player_name}: {allocated}/{stake} tix ({percentage:.1f}%)")
                
                # Step 3: Final allocations
                allocation_text.append("\n**Step 3: Final Allocations**")

                # Min team allocations section
                allocation_text.append(f"*{min_team_name} (Min Team):*")
                # Sort min team players by original stake
                sorted_min_team = sorted([(pid, capped_stakes.get(pid, 0), original_stakes.get(pid, 0)) 
                                        for pid in min_team if pid in original_stakes], 
                                        key=lambda x: x[2], reverse=True)

                for player_id, capped_stake, original_stake in sorted_min_team:
                    player_name = player_names.get(player_id, "Unknown")
                    allocated = final_allocations.get(player_id, 0)
                    
                    # Calculate percentage of their max bet
                    percentage = (allocated / original_stake * 100) if original_stake > 0 else 0
                    
                    # Show allocation compared to their max bet
                    if capped_stake < original_stake:
                        allocation_text.append(f"{player_name}: {allocated}/{original_stake} tix ({percentage:.1f}%) [capped to {capped_stake} tix]")
                    else:
                        allocation_text.append(f"{player_name}: {allocated}/{original_stake} tix ({percentage:.1f}%)")

                # Max team allocations section
                allocation_text.append(f"\n*{max_team_name} (Max Team):*")
                # Sort max team players by original stake
                sorted_max_team = sorted([(pid, capped_stakes.get(pid, 0), original_stakes.get(pid, 0)) 
                                        for pid in max_team if pid in original_stakes], 
                                        key=lambda x: x[2], reverse=True)

                for player_id, capped_stake, original_stake in sorted_max_team:
                    player_name = player_names.get(player_id, "Unknown")
                    allocated = final_allocations.get(player_id, 0)
                    
                    # Calculate percentage of their max bet
                    percentage = (allocated / original_stake * 100) if original_stake > 0 else 0
                    
                    # Show allocation compared to their max bet
                    if capped_stake < original_stake:
                        allocation_text.append(f"{player_name}: {allocated}/{original_stake} tix ({percentage:.1f}%) [capped to {capped_stake} tix]")
                    else:
                        allocation_text.append(f"{player_name}: {allocated}/{original_stake} tix ({percentage:.1f}%)")
                
        else:
            # For proportional approach - keeping original logic
            allocation_text.append("**Step 1: Calculate Highest Min Team Bet**")
            highest_min_team_bet = max([capped_stakes.get(p, 0) for p in min_team if p in capped_stakes])
            min_stake_value = min([s for s in capped_stakes.values() if s > 0])
            
            allocation_text.append(f"Highest Min Team Bet = {highest_min_team_bet} tix")
            
            # Step 2: Apply caps to high bettors based on highest min team bet
            allocation_text.append("\n**Step 2: Apply Highest Min Team Bet Cap**")
            for player_id, original in sorted([(p, capped_stakes.get(p, 0)) for p in max_team if p in capped_stakes], 
                                            key=lambda x: x[1], reverse=True):
                player_name = player_names.get(player_id, "Unknown")
                if original > highest_min_team_bet:
                    allocation_text.append(f"{player_name}: {original} → {highest_min_team_bet} tix (capped by algorithm)")
            
            # Step 3: Calculate proportional allocation
            allocation_text.append("\n**Step 3: Calculate Max Team Proportional Allocation**")
            
            # Rest of proportional approach logic
            # Identify min bettors on max team (players betting exactly min_stake_value)
            min_bettors = [(p, capped_stakes.get(p, 0)) for p in max_team 
                        if p in capped_stakes and capped_stakes.get(p, 0) == min_stake_value]
            min_bettors_total = sum(stake for _, stake in min_bettors)

            # Identify all other max team players (those betting more than min_stake_value)
            above_min_bettors = [(p, min(capped_stakes.get(p, 0), highest_min_team_bet)) for p in max_team 
                                if p in capped_stakes and capped_stakes.get(p, 0) > min_stake_value]
            above_min_bettors.sort(key=lambda x: x[1], reverse=True)  # Sort by stake amount

            # Calculate remaining capacities
            remaining_min_capacity = min_team_total - min_bettors_total
            remaining_max_capacity = sum(stake for _, stake in above_min_bettors)

            # Calculate the proportional allocation percentage
            if remaining_max_capacity > 0:
                proportional_percentage = (remaining_min_capacity / remaining_max_capacity) * 100
                proportional_percentage_capped = min(proportional_percentage, 100)
            else:
                proportional_percentage = 100
                proportional_percentage_capped = 100

            allocation_text.append(f"Reserved Bet for Min Bettors: {min_bettors_total} tix")
            allocation_text.append(f"Remaining Min Team Capacity: {min_team_total} - {min_bettors_total} = {remaining_min_capacity} tix")
            allocation_text.append(f"Remaining Max Team Capacity: {remaining_max_capacity} tix")
            allocation_text.append(f"Proportional Allocation: {remaining_min_capacity}/{remaining_max_capacity} = {proportional_percentage_capped:.0f}%\n")

            # Calculate and show the allocation for each above-min bettor
            allocation_text.append("Apply Percentage to Max Bets (round to nearest 10):")
            total_allocated_before_rounding = 0
            total_allocated_after_rounding = 0

            for player_id, stake in above_min_bettors:
                player_name = player_names.get(player_id, "Unknown")
                capped_stake = min(stake, highest_min_team_bet)  # Using highest min team bet
                
                # Calculate raw allocation before rounding
                raw_allocation = capped_stake * proportional_percentage_capped / 100
                total_allocated_before_rounding += raw_allocation
                
                # Round to nearest 10
                rounded_allocation = round(raw_allocation / 10) * 10
                # Ensure minimum is met
                rounded_allocation = max(rounded_allocation, min_stake_value)
                # Ensure we don't exceed the capped stake
                rounded_allocation = min(rounded_allocation, capped_stake)
                
                total_allocated_after_rounding += rounded_allocation
                
                # Display the calculation
                allocation_text.append(f"{player_name}: {capped_stake} × {proportional_percentage_capped:.0f}% = {rounded_allocation} tix")

            # Show rounding adjustments if needed
            adjustment_needed = remaining_min_capacity - total_allocated_after_rounding
            if abs(adjustment_needed) >= 10:  # Use 10 as the minimum adjustment to show
                allocation_text.append(f"\nRounding Adjustment Needed: {adjustment_needed} tix")
                
                if adjustment_needed > 0:
                    allocation_text.append("Adding adjustment to highest bettors")
                else:
                    allocation_text.append("Removing adjustment from lowest bettors")
            
            # Step 4: Final allocations
            allocation_text.append("\n**Step 4: Final Allocations**")
            
            # Min team allocations
            allocation_text.append(f"*{min_team_name} (Min Team):*")
            for player_id in min_team:
                if player_id in capped_stakes:
                    player_name = player_names.get(player_id, "Unknown")
                    stake = capped_stakes.get(player_id, 0)
                    allocated = final_allocations.get(player_id, 0)
                    percentage = (allocated / stake * 100) if stake > 0 else 0
                    allocation_text.append(f"{player_name}: {allocated}/{stake} tix ({percentage:.1f}%)")
            
            # Max team allocations
            allocation_text.append(f"\n*{max_team_name} (Max Team):*")
            for player_id, original in sorted([(p, capped_stakes.get(p, 0)) for p in max_team if p in capped_stakes], 
                                            key=lambda x: x[1], reverse=True):
                player_name = player_names.get(player_id, "Unknown")
                stake = min(original, highest_min_team_bet)  # Apply highest min team bet cap
                allocated = final_allocations.get(player_id, 0)
                percentage = (allocated / original * 100) if original > 0 else 0
                allocation_text.append(f"{player_name}: {allocated}/{original} tix ({percentage:.1f}%)")
        
        # Add allocation text as a field - handle length by splitting if necessary
        if len("\n".join(allocation_text)) > 1024:
            # If allocation text is too long, split it into multiple fields
            first_part = allocation_text[:len(allocation_text)//2]
            second_part = allocation_text[len(allocation_text)//2:]
            
            embed3.add_field(
                name="Allocation Phase (Part 1)",
                value="\n".join(first_part),
                inline=False
            )
            
            embed3.add_field(
                name="Allocation Phase (Part 2)",
                value="\n".join(second_part),
                inline=False
            )
        else:
            embed3.add_field(
                name="Allocation Phase",
                value="\n".join(allocation_text),
                inline=False
            )
        
        # Bet Matching Phase - SAME AS ORIGINAL
        # If stake lines are too long, truncate
        stake_lines_text = "\n".join(stake_lines) if stake_lines else "No pairings created"
        if len(stake_lines_text) > 1024:
            # Truncate and add a note
            stake_lines_to_show = stake_lines[:10]  # Show only first 10 lines
            stake_lines_text = "\n".join(stake_lines_to_show) + f"\n\n... and {len(stake_lines) - 10} more pairings"
        
        embed3.add_field(
            name=f"Bet Matching Phase Results (Total: {sum(final_allocations.values())//2} tix)",
            value=stake_lines_text,
            inline=False
        )
        
        embeds.append(embed3)
        
        return embeds

class PersonalizedCapStatusView(discord.ui.View):
    def __init__(self, draft_session_id, user_id):
        super().__init__(timeout=None)
        self.draft_session_id = draft_session_id
        self.user_id = user_id
        
        # Add toggle button
        self.toggle_button = discord.ui.Button(
            label="Toggle Cap Status",
            style=discord.ButtonStyle.secondary,
            custom_id=f"toggle_bet_cap_{draft_session_id}"
        )
        self.toggle_button.callback = self.toggle_cap_callback
        self.add_item(self.toggle_button)
    
    async def toggle_cap_callback(self, interaction: discord.Interaction):
        user_id = self.user_id
        
        # Only the owner of the view should be able to toggle
        if str(interaction.user.id) != user_id:
            await interaction.response.send_message("This button is not for you.", ephemeral=True)
            return
            
        # Update stake info in database
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Get the user's stake info
                stake_stmt = select(StakeInfo).where(and_(
                    StakeInfo.session_id == self.draft_session_id,
                    StakeInfo.player_id == user_id
                ))
                stake_result = await session.execute(stake_stmt)
                stake_info = stake_result.scalars().first()
                
                if not stake_info:
                    await interaction.response.send_message("You need to set a stake amount first.", ephemeral=True)
                    return
                
                # Toggle the capping status
                current_cap_status = getattr(stake_info, 'is_capped', True)
                stake_info.is_capped = not current_cap_status
                
                # Update the database
                session.add(stake_info)
                await session.commit()
        
                # Create an updated view
                new_status = "ON 🧢" if stake_info.is_capped else "OFF 🏎️"
                style = discord.ButtonStyle.green if stake_info.is_capped else discord.ButtonStyle.red
                
                updated_view = discord.ui.View(timeout=None)
                status_button = discord.ui.Button(
                    label=f"Bet Cap: {new_status}",
                    style=style,
                    custom_id=f"bet_cap_status_{self.draft_session_id}",
                    disabled=True
                )
                updated_view.add_item(status_button)
                
                # Add the toggle button back
                toggle_button = discord.ui.Button(
                    label="Toggle Cap Status",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"toggle_bet_cap_{self.draft_session_id}"
                )
                toggle_button.callback = self.toggle_cap_callback
                updated_view.add_item(toggle_button)
                
                await interaction.response.edit_message(
                    content=f"Your bet cap status is now: {new_status}.\n" +
                    ("Your bet will be capped at the highest opponent bet." if stake_info.is_capped else 
                     "Your bet will NOT be capped by the opposing team's highest bet and may be spread across multiple opponents."),
                    view=updated_view
                )
                
                # Update the draft message
                await update_draft_message(interaction.client, self.draft_session_id)

async def show_personalized_cap_status(interaction, draft_session_id):
    """Shows a personalized cap status button for the user"""
    user_id = str(interaction.user.id)
    
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Get the user's stake info
            stake_stmt = select(StakeInfo).where(and_(
                StakeInfo.session_id == draft_session_id,
                StakeInfo.player_id == user_id
            ))
            stake_result = await session.execute(stake_stmt)
            stake_info = stake_result.scalars().first()
            
            if not stake_info:
                await interaction.response.send_message("You need to set a stake amount first.", ephemeral=True)
                return
            
            # Create the personalized view
            is_capped = getattr(stake_info, 'is_capped', True)
            status = "ON 🧢" if is_capped else "OFF 🏎️"
            style = discord.ButtonStyle.green if is_capped else discord.ButtonStyle.red
            
            view = discord.ui.View(timeout=None)
            status_button = discord.ui.Button(
                label=f"Bet Cap: {status}",
                style=style,
                custom_id=f"bet_cap_status_{draft_session_id}",
                disabled=True
            )
            view.add_item(status_button)
            
            # Add the toggle button 
            toggle_button = discord.ui.Button(
                label="Toggle Cap Status",
                style=discord.ButtonStyle.secondary,
                custom_id=f"toggle_bet_cap_{draft_session_id}"
            )
            
            # Define callback for the toggle button
            async def toggle_callback(interaction):
                await show_personalized_cap_status(interaction, draft_session_id)
            
            toggle_button.callback = toggle_callback
            view.add_item(toggle_button)
            
            await interaction.response.send_message(
                f"Your bet cap status is: {status}.\n" +
                ("Your bet will be capped at the highest opponent bet." if is_capped else 
                 "Your bet will NOT be capped by the opposing team's highest bet and may be spread across multiple opponents."),
                view=view,
                ephemeral=True
            )

class BetCapToggleButton(CallbackButton):
    def __init__(self, draft_session_id):
        super().__init__(
            label="Change Bet/Settings",
            style=discord.ButtonStyle.secondary,
            custom_id=f"bet_cap_toggle_{draft_session_id}",
            custom_callback=self.bet_cap_callback
        )
        self.draft_session_id = draft_session_id
    
    async def bet_cap_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)
        
        # Check if user is registered in this draft
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Get the draft session
                draft_stmt = select(DraftSession).where(DraftSession.session_id == self.draft_session_id)
                draft_result = await session.execute(draft_stmt)
                draft_session = draft_result.scalars().first()
                
                if not draft_session:
                    await interaction.response.send_message("Draft session not found.", ephemeral=True)
                    return
                    
                # Check if user is in the draft
                if user_id not in draft_session.sign_ups:
                    await interaction.response.send_message("You're not registered for this draft.", ephemeral=True)
                    return
                
                # Get the user's stake info
                stake_stmt = select(StakeInfo).where(and_(
                    StakeInfo.session_id == self.draft_session_id,
                    StakeInfo.player_id == user_id
                ))
                stake_result = await session.execute(stake_stmt)
                stake_info = stake_result.scalars().first()
                
                if not stake_info:
                    await interaction.response.send_message("You need to set a stake amount first.", ephemeral=True)
                    return
                
                # Get user's current status
                is_capped = getattr(stake_info, 'is_capped', True)  # Default to True if not set
                status = "ON 🧢" if is_capped else "OFF 🏎️"
                style = discord.ButtonStyle.green if is_capped else discord.ButtonStyle.red
                current_stake = stake_info.max_stake
                
                # Create combined view with both stake options and cap buttons
                combined_view = discord.ui.View(timeout=None)
                
                # Create a custom view that combines stake options and cap settings
                # First add the stake options dropdown
                min_stake = draft_session.min_stake or 10
                options = []
                if min_stake <= 10:
                    options.append(discord.SelectOption(label="10 TIX", value="10"))
                if min_stake <= 20:
                    options.append(discord.SelectOption(label="20 TIX", value="20"))
                if min_stake <= 50:   
                    options.append(discord.SelectOption(label="50 TIX", value="50"))
                if min_stake <= 100:   
                    options.append(discord.SelectOption(label="100 TIX", value="100"))
                options.append(discord.SelectOption(label="Over 100 TIX", value="over_100"))
                
                # Create the stake options dropdown
                stake_select = CombinedStakeSelect(
                    draft_session_id=self.draft_session_id,
                    draft_link=draft_session.draft_link,
                    user_display_name=interaction.user.display_name,
                    min_stake=min_stake,
                    current_stake=current_stake,
                    options=options
                )
                combined_view.add_item(stake_select)
                
                # Add bet cap status button (informational only)
                status_button = discord.ui.Button(
                    label=f"Bet Cap: {status}",
                    style=style,
                    custom_id=f"bet_cap_status_{self.draft_session_id}",
                    disabled=True
                )
                combined_view.add_item(status_button)
                
                # Create ON button with its callback
                yes_button = discord.ui.Button(
                    label="Turn ON 🧢",
                    style=discord.ButtonStyle.green,
                    custom_id=f"cap_yes_{self.draft_session_id}"
                )
                
                async def yes_callback(yes_interaction):
                    if yes_interaction.user.id != interaction.user.id:
                        await yes_interaction.response.send_message("This button is not for you.", ephemeral=True)
                        return
                    
                    # Update cap status
                    await self.update_cap_status(yes_interaction, user_id, guild_id, is_capped=True)
                
                yes_button.callback = yes_callback
                combined_view.add_item(yes_button)
                
                # Create OFF button with its callback
                no_button = discord.ui.Button(
                    label="Turn OFF 🏎️", 
                    style=discord.ButtonStyle.red,
                    custom_id=f"cap_no_{self.draft_session_id}"
                )
                
                async def no_callback(no_interaction):
                    if no_interaction.user.id != interaction.user.id:
                        await no_interaction.response.send_message("This button is not for you.", ephemeral=True)
                        return
                    
                    # Update cap status
                    await self.update_cap_status(no_interaction, user_id, guild_id, is_capped=False)
                
                no_button.callback = no_callback
                combined_view.add_item(no_button)
                
                # Send the ephemeral message with the combined view
                message_content = f"Your current bet is {current_stake} tix with bet cap {status}.\n"
                message_content += f"Min Bet for queue is {min_stake}. Select a new max bet and/or adjust your cap settings.\n"
                message_content += "Your bet cap preferences will be saved for future drafts."
                
                await interaction.response.send_message(
                    content=message_content,
                    view=combined_view,
                    ephemeral=True
                )
    
    async def update_cap_status(self, interaction, user_id, guild_id, is_capped):
        """Helper method to update cap status"""
        async with AsyncSessionLocal() as inner_session:
            async with inner_session.begin():
                # Get the stake info
                inner_stake_stmt = select(StakeInfo).where(and_(
                    StakeInfo.session_id == self.draft_session_id,
                    StakeInfo.player_id == user_id
                ))
                inner_stake_result = await inner_session.execute(inner_stake_stmt)
                inner_stake_info = inner_stake_result.scalars().first()
                
                if not inner_stake_info:
                    await interaction.response.send_message("Error: Stake info not found.", ephemeral=True)
                    return
                
                # Set is_capped status
                inner_stake_info.is_capped = is_capped
                inner_session.add(inner_stake_info)
                await inner_session.commit()
        
        # Update the player's preference for future drafts
        from preference_service import update_player_bet_capping_preference
        await update_player_bet_capping_preference(user_id, guild_id, is_capped)
        
        # Update the draft message
        await update_draft_message(interaction.client, self.draft_session_id)
        
        # Inform the user
        status_text = "ON 🧢" if is_capped else "OFF 🏎️"
        description_text = "capped at the highest opponent bet" if is_capped else "NOT capped and may be spread across multiple opponents"
        
        await interaction.response.send_message(
            f"Your bet cap has been turned {status_text}. Your bet will be {description_text}.\n\nThis preference will be remembered for future drafts.",
            ephemeral=True
        )

class CombinedStakeSelect(discord.ui.Select):
    def __init__(self, draft_session_id, draft_link, user_display_name, min_stake, current_stake, options):
        self.draft_session_id = draft_session_id
        self.draft_link = draft_link
        self.user_display_name = user_display_name
        self.min_stake = min_stake
        self.current_stake = current_stake
        
        # Set placeholder to show current stake
        placeholder = f"Current Bet: {current_stake} tix - Select new max bet..."
        
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)
        
    async def callback(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)
        
        # Load the user's saved preference
        from preference_service import get_player_bet_capping_preference
        is_capped = await get_player_bet_capping_preference(user_id, guild_id)
        
        selected_value = self.values[0]
        
        if selected_value == "over_100":
            # Create modal for custom amount over 100
            stake_modal = StakeModal(over_100=True)
            stake_modal.draft_session_id = self.draft_session_id
            stake_modal.draft_link = self.draft_link
            stake_modal.user_display_name = self.user_display_name
            
            # IMPORTANT: Set the default value based on saved preference before showing the modal
            stake_modal.default_cap_setting = is_capped
            # Update the cap checkbox value based on the preference
            stake_modal.cap_checkbox.value = "yes" if is_capped else "no"
            
            await interaction.response.send_modal(stake_modal)
        else:
            # Process the selected preset stake amount with the saved preference
            stake_amount = int(selected_value)
            
            # Add user to sign_ups and handle stake submission using saved preference
            await self.handle_stake_submission(interaction, stake_amount, is_capped=is_capped)
            
    async def handle_stake_submission(self, interaction, stake_amount, is_capped=True):
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)
        
        # Only proceed if this is different from current stake
        if stake_amount == self.current_stake:
            await interaction.response.send_message(f"You already have a {stake_amount} tix stake set.", ephemeral=True)
            return
            
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Get the draft session
                draft_stmt = select(DraftSession).where(DraftSession.session_id == self.draft_session_id)
                draft_result = await session.execute(draft_stmt)
                draft_session = draft_result.scalars().first()
                
                if not draft_session:
                    await interaction.response.send_message("Draft session not found.", ephemeral=True)
                    return
                
                # Update sign_ups (ensure user is in the sign_ups)
                sign_ups = draft_session.sign_ups or {}
                sign_ups[user_id] = interaction.user.display_name
                
                await session.execute(
                    update(DraftSession).
                    where(DraftSession.session_id == self.draft_session_id).
                    values(sign_ups=sign_ups)
                )
                
                # Check if a stake record already exists for this player
                stake_stmt = select(StakeInfo).where(and_(
                    StakeInfo.session_id == self.draft_session_id,
                    StakeInfo.player_id == user_id
                ))
                stake_result = await session.execute(stake_stmt)
                stake_info = stake_result.scalars().first()
                
                if stake_info:
                    # Update existing stake
                    stake_info.max_stake = stake_amount
                    stake_info.is_capped = is_capped
                else:
                    # Create new stake record
                    stake_info = StakeInfo(
                        session_id=self.draft_session_id,
                        player_id=user_id,
                        max_stake=stake_amount,
                        is_capped=is_capped
                    )
                    session.add(stake_info)
                
                await session.commit()
        
        # Confirm stake and provide draft link
        cap_status = "capped at the highest opponent bet" if is_capped else "NOT capped (full action)"
        signup_message = f"You've updated your maximum bet to {stake_amount} tix."
        signup_message += f"\nYour bet will be {cap_status}."
            
        # Send confirmation message
        await interaction.response.send_message(signup_message, ephemeral=True)
        
        # Update the draft message to reflect the new list of sign-ups
        await update_draft_message(interaction.client, self.draft_session_id)