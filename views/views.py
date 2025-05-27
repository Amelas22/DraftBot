"""
Main views module for the draft bot.
This module contains the PersistentView class and related functionality.
"""

import discord
import asyncio
import random
import pytz
from datetime import datetime, timedelta
from discord.ui import Button, View
from loguru import logger
from sqlalchemy import update, select, and_
from sqlalchemy.orm import selectinload

from config import TEST_MODE_ENABLED, get_config
from views.view_helpers import (
    BaseView, CallbackButton, ResponseHelper, DatabaseHelper, 
    EmbedHelper, PermissionHelper, ProcessingLockManager, CooldownManager,
    ButtonStateManager
)
from views.ready_check_views import ReadyCheckManager, READY_CHECK_SESSIONS
from views.stake_views import (
    StakeOptionsView, StakeCalculationButton, BetCapToggleButton,
    StakeOptionsSelect, StakeModal
)
from views.match_result_views import create_pairings_view
from views.user_management_views import UserRemovalView, CancelConfirmationView
from views.draft_message_utils import update_draft_message, update_last_draft_timestamp

from session import AsyncSessionLocal, get_draft_session, DraftSession, StakeInfo
from services.draft_setup_manager import DraftSetupManager
from cube_views.CubeSelectionView import CubeUpdateSelectionView
from draft_organization.stake_calculator import calculate_stakes_with_strategy
from utils import (
    calculate_pairings, get_formatted_stake_pairs, generate_draft_summary_embed,
    post_pairings, generate_seating_order, split_into_teams, 
    check_weekly_limits, update_player_stats_for_draft, get_missing_stake_players
)


# Global states
PROCESSING_ROOMS_PAIRINGS = {}
PROCESSING_TEAMS_CREATION = {}


class PersistentView(BaseView):
    """
    Main persistent view for draft management.
    Handles all draft-related interactions and button callbacks.
    """
    
    def __init__(self, bot, draft_session_id: str, session_type: str = None, 
                 team_a_name: str = None, team_b_name: str = None, 
                 session_stage: str = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.draft_session_id = draft_session_id
        self.session_type = session_type
        self.team_a_name = team_a_name
        self.team_b_name = team_b_name
        self.session_stage = session_stage
        self.channel_ids = []
        self.draft_chat_channel = None
        self._add_buttons()

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

    def _add_buttons(self):
        """Add all necessary buttons based on session type and stage."""
        # Sign-up buttons for non-premade sessions
        if self.session_type != "premade":
            self._add_button("Sign Up", "green", "sign_up", self.sign_up_callback)
            self._add_button("Cancel Sign Up", "red", "cancel_sign_up", self.cancel_sign_up_callback)

        # Shared buttons
        self._add_shared_buttons()

        # Type-specific buttons
        if self.session_type == "winston":
            self._add_winston_buttons()
        elif self.session_type == "premade":
            self._add_premade_buttons()
        else:
            self._add_generic_buttons()

        # Staked draft specific
        if self.session_type == "staked":
            self.add_item(BetCapToggleButton(self.draft_session_id))

        # Ready check and rooms buttons (not for test sessions)
        if self.session_type != "test":
            self._add_button("Ready Check", "green", "ready_check", self.ready_check_callback)
            self._add_button("Create Rooms & Post Pairings", "primary", 
                           "create_rooms_pairings", self.create_rooms_pairings_callback, 
                           disabled=True)

        # Apply stage-based button disabling
        self._apply_stage_button_disabling()

    def _add_button(self, label: str, style: str, custom_id_suffix: str, 
                   callback, **kwargs):
        """Helper to add a button with standardized custom_id format."""
        button = self._create_button(label, style, 
                                   f"{custom_id_suffix}_{self.draft_session_id}", 
                                   callback, **kwargs)
        self.add_item(button)

    def _create_button(self, label: str, style: str, custom_id: str, 
                      custom_callback, disabled: bool = False) -> CallbackButton:
        """Create a button with the specified properties."""
        style_obj = getattr(discord.ButtonStyle, style)
        return CallbackButton(
            label=label, 
            style=style_obj, 
            custom_id=custom_id, 
            custom_callback=custom_callback, 
            disabled=disabled
        )

    def _add_shared_buttons(self):
        """Add buttons that are shared across all session types."""
        self._add_button("Cancel Draft", "grey", "cancel_draft", self.cancel_draft_callback)
        self._add_button("Remove User", "grey", "remove_user", self.remove_user_callback)
        self._add_button("Update Cube", "blurple", "update_cube", self.update_cube_callback)

    def _add_winston_buttons(self):
        """Add buttons specific to Winston drafts."""
        self._add_button("Start Draft", "green", "start_draft", self.start_draft_callback)

    def _add_premade_buttons(self):
        """Add buttons specific to premade drafts."""
        self._add_button(self.team_a_name, "green", "Team_A", self.team_assignment_callback)
        self._add_button(self.team_b_name, "red", "Team_B", self.team_assignment_callback)
        self._add_button("Generate Seating Order", "primary", "generate_seating", 
                       self.randomize_teams_callback)

    def _add_generic_buttons(self):
        """Add buttons for generic draft types."""
        if self.session_type == "swiss":
            self._add_button("Generate Seating Order", "blurple", "randomize_teams", 
                           self.randomize_teams_callback)
        elif self.session_type not in {"test", "schedule"}:
            self._add_button("Create Teams", "blurple", "randomize_teams", 
                           self.randomize_teams_callback)

        # Stake explanation button
        if self.session_type == "staked" and self.session_stage != "teams":
            self._add_button("How Bets Work 💰", "green", "explain_stakes", 
                           self.explain_stakes_callback)
        
        # Test users button (only in test mode)
        if TEST_MODE_ENABLED:
            self._add_button("🧪 Add Test Users", "grey", "add_test_users", 
                           self.add_test_users_callback)

    def _apply_stage_button_disabling(self):
        """Disable buttons based on the current session stage."""
        if self.session_stage == "teams":
            ButtonStateManager.disable_all_except(
                self, 
                ["create_rooms_pairings", "cancel_draft"]
            )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if the interaction is valid (session exists)."""
        session_exists = await get_draft_session(self.draft_session_id) is not None
        if not session_exists:
            await ResponseHelper.send_error(
                interaction, 
                "The draft session could not be found."
            )
        return session_exists

    # ========== Sign-up Related Callbacks ==========
    
    async def sign_up_callback(self, interaction: discord.Interaction, button: Button):
        """Handle user sign-up for the draft."""
        user_id = str(interaction.user.id)
        
        # Check timeout role
        if await PermissionHelper.check_timeout_role(interaction):
            await ResponseHelper.send_error(
                interaction,
                "You are ineligible to join a queue due to an infraction "
                "(Leaving Draft Early/Unpaid Debts). Message a Mod for more details."
            )
            return
        
        # Get draft session
        draft_session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if not draft_session:
            await ResponseHelper.send_error(interaction, "The draft session could not be found.")
            return
        
        # Check weekly limits for swiss
        if draft_session.session_type == "swiss":
            if not await self._check_weekly_limits(interaction, user_id):
                return
        
        # Check if already signed up
        sign_ups = draft_session.sign_ups or {}
        if user_id in sign_ups:
            await ResponseHelper.send_error(interaction, "You are already signed up!")
            return
        
        # Handle staked drafts separately
        if self.session_type == "staked":
            await self._handle_staked_signup(interaction, draft_session)
        else:
            await self._handle_regular_signup(interaction, draft_session)

    async def _handle_staked_signup(self, interaction: discord.Interaction, 
                                   draft_session: DraftSession):
        """Handle sign-up for staked drafts."""
        has_draftmancer_role = discord.utils.get(
            interaction.user.roles, 
            name="Draftmancer"
        ) is not None
        
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

    async def _handle_regular_signup(self, interaction: discord.Interaction, 
                                    draft_session: DraftSession):
        """Handle sign-up for regular drafts."""
        user_id = str(interaction.user.id)
        display_name = interaction.user.display_name
        
        # Update sign-ups
        sign_ups = draft_session.sign_ups or {}
        sign_ups[user_id] = display_name
        
        # Check for Draftmancer role
        has_draftmancer_role = discord.utils.get(
            interaction.user.roles, 
            name="Draftmancer"
        ) is not None
        
        draftmancer_role_users = draft_session.draftmancer_role_users or []
        if has_draftmancer_role and display_name not in draftmancer_role_users:
            draftmancer_role_users.append(display_name)
        
        # Check if we should ping
        should_ping = await self._check_should_ping(draft_session, len(sign_ups))
        
        # Update database
        update_values = {
            "sign_ups": sign_ups,
            "draftmancer_role_users": draftmancer_role_users
        }
        
        if should_ping:
            update_values["should_ping"] = True
        
        if not draft_session.session_stage:
            update_values["deletion_time"] = datetime.now() + timedelta(minutes=180)
        
        await DatabaseHelper.update_draft_session(self.draft_session_id, **update_values)
        
        # Send confirmation
        draft_link = draft_session.get_draft_link_for_user(display_name)
        await ResponseHelper.send_success(
            interaction, 
            f"You are now signed up. Join Here: {draft_link}"
        )
        
        # Send ping if needed
        if should_ping:
            await self._send_draft_ping(interaction, draft_session, len(sign_ups))
        
        # Update draft message
        await update_draft_message(interaction.client, self.draft_session_id)
        
        # Handle winston draft specifics
        if self.session_type == "winston":
            await self._handle_winston_signup_announcement(interaction, draft_session, sign_ups)

    async def cancel_sign_up_callback(self, interaction: discord.Interaction, button: Button):
        """Handle user cancelling their sign-up."""
        draft_session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if not draft_session:
            await ResponseHelper.send_error(interaction, "The draft session could not be found.")
            return
        
        sign_ups = draft_session.sign_ups or {}
        user_id = str(interaction.user.id)
        display_name = str(interaction.user.display_name)
        
        if user_id not in sign_ups:
            await ResponseHelper.send_error(interaction, "You are not signed up!")
            return
        
        # Remove from sign-ups
        del sign_ups[user_id]
        
        # Remove from draftmancer users if present
        draftmancer_role_users = draft_session.draftmancer_role_users or []
        if display_name in draftmancer_role_users:
            draftmancer_role_users.remove(display_name)
        
        # Update database
        await DatabaseHelper.update_draft_session(
            self.draft_session_id,
            sign_ups=sign_ups,
            draftmancer_role_users=draftmancer_role_users
        )
        
        await ResponseHelper.send_success(interaction, "Your sign up has been canceled!")
        await update_draft_message(interaction.client, self.draft_session_id)

    # ========== Team Management Callbacks ==========
    
    async def randomize_teams_callback(self, interaction: discord.Interaction, button: Button):
        """Handle team creation/randomization."""
        # Use processing lock to prevent race conditions
        async def create_teams():
            return await self._create_teams_internal(interaction)
        
        result = await ProcessingLockManager.with_lock(
            'teams_creation', 
            self.draft_session_id, 
            create_teams(),
            interaction
        )
        
        if result is False:  # Lock couldn't be acquired
            return

    async def _create_teams_internal(self, interaction: discord.Interaction):
        """Internal method for creating teams."""
        user_id = str(interaction.user.id)
        
        # Verify user is in queue
        draft_session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if not draft_session or user_id not in (draft_session.sign_ups or {}):
            await ResponseHelper.send_error(
                interaction, 
                "You are not eligible to create teams as you are not in the queue."
            )
            return
        
        # Defer response
        await interaction.response.defer()
        
        # Additional checks for staked drafts
        if self.session_type == "staked":
            if not await self._validate_staked_draft_ready(interaction):
                return
        
        # Validate even number of players
        if len(draft_session.sign_ups) % 2 != 0:
            await interaction.followup.send("There must be an even number of players to fire.")
            return
        
        # Create teams
        await self._execute_team_creation(interaction, draft_session)

    async def _validate_staked_draft_ready(self, interaction: discord.Interaction) -> bool:
        """Validate that a staked draft is ready for team creation."""
        # Check ready check was performed
        if self.draft_session_id not in READY_CHECK_SESSIONS:
            await interaction.followup.send(
                "You must perform a Ready Check before creating teams for a money draft.",
                ephemeral=True
            )
            return False
        
        # Check all players have stakes set
        from utils import get_missing_stake_players
        missing_players = await get_missing_stake_players(self.draft_session_id)
        
        if missing_players:
            missing_names = await self._get_player_names(interaction.guild, missing_players)
            await interaction.followup.send(
                f"Cannot create teams yet. The following players need to set "
                f"their stakes: {', '.join(missing_names)}",
                ephemeral=True
            )
            return False
        
        return True

    async def _execute_team_creation(self, interaction: discord.Interaction, 
                                    draft_session: DraftSession):
        """Execute the team creation process."""
        # Update session stage and timers
        update_values = {
            "teams_start_time": datetime.now(),
            "deletion_time": (datetime.now() + timedelta(days=7) 
                            if draft_session.session_type == 'premade' 
                            else datetime.now() + timedelta(hours=4)),
            "session_stage": 'teams'
        }
        
        await DatabaseHelper.update_draft_session(self.draft_session_id, **update_values)
        
        # Create teams for appropriate session types
        if self.session_type in ('random', 'test', 'staked'):
            await split_into_teams(interaction.client, self.draft_session_id)
            
            # Clear ready check data
            ReadyCheckManager.clear_ready_check(self.draft_session_id)
            
            # Handle staked draft stake calculations
            if self.session_type == "staked":
                updated_session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
                if updated_session and updated_session.team_a and updated_session.team_b:
                    await self._calculate_stakes_for_teams(interaction, updated_session)
        
        # Generate and send team announcement
        await self._announce_teams(interaction)
        
        # Update button states
        ButtonStateManager.disable_all_except(self, ["create_rooms_pairings", "cancel_draft"])
        
        # Notify draft manager if exists
        await self._notify_draft_manager(interaction)
        
        # Check weekly limits for tracked drafts
        updated_session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if updated_session.tracked_draft and updated_session.premade_match_id:
            await check_weekly_limits(
                interaction, 
                updated_session.premade_match_id, 
                updated_session.session_type, 
                updated_session.session_id
            )

    # ========== Room Creation Callbacks ==========
    

    async def create_rooms_pairings_callback(self, interaction: discord.Interaction, button: Button):
        """Handle room creation and pairing posting."""
        async def create_rooms():
            await interaction.response.defer()
            
            # Disable the button
            ButtonStateManager.update_button_by_id(
                self, 
                "create_rooms_pairings", 
                disabled=True
            )
            
            result = await PersistentView.create_rooms_pairings(
                interaction.client, 
                interaction.guild, 
                self.draft_session_id, 
                interaction
            )
            
            if result:
                try:
                    # Try to edit the original message
                    await self.message.edit(view=self)
                except discord.NotFound:
                    # Message was deleted, try to update via interaction instead
                    logger.warning(f"Original message not found for session {self.draft_session_id}, attempting interaction update")
                    try:
                        await interaction.edit_original_response(view=self)
                    except Exception as e:
                        logger.error(f"Failed to update message via interaction: {e}")
                        # If both fail, at least log that the operation completed successfully
                        logger.info(f"Unable to find original message")
                except discord.HTTPException as e:
                    logger.error(f"HTTP error updating message for session {self.draft_session_id}: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error updating message for session {self.draft_session_id}: {e}")
            
            return result
        
        await ProcessingLockManager.with_lock(
            'rooms_pairings',
            self.draft_session_id,
            create_rooms(),
            interaction
        )

    # ========== Other Callbacks ==========
    
    async def ready_check_callback(self, interaction: discord.Interaction, button: Button):
        """Handle ready check initiation."""
        result = await ReadyCheckManager.initiate_ready_check(
            interaction, 
            self.draft_session_id, 
            self
        )
        
        if not result:
            logger.debug(f"Ready check failed for session {self.draft_session_id}")

    async def explain_stakes_callback(self, interaction: discord.Interaction, button: Button):
        """Explain how the stake system works."""
        from views.stake_explanation import create_stake_explanation_embed
        embed = create_stake_explanation_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def update_cube_callback(self, interaction: discord.Interaction, button: Button):
        """Handle cube update request."""
        draft_session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if not draft_session:
            await ResponseHelper.send_error(interaction, "The draft session could not be found.")
            return
        
        cube_selection = CubeUpdateSelectionView(draft_session.session_type)
        
        # Create custom callback for cube selection
        async def custom_cube_callback(select_interaction):
            await self._handle_cube_update(select_interaction, draft_session)
        
        cube_selection.cube_select.callback = custom_cube_callback
        
        await interaction.response.send_message(
            f"Select a new cube for this draft (currently using {draft_session.cube}):", 
            view=cube_selection, 
            ephemeral=True
        )

    async def cancel_draft_callback(self, interaction: discord.Interaction, button: Button):
        """Handle draft cancellation request."""
        if await PermissionHelper.check_timeout_role(interaction):
            await ResponseHelper.send_error(
                interaction,
                "You are ineligible due to an infraction "
                "(Leaving Draft Early/Unpaid Debts). Message a Mod for more details."
            )
            return
        
        draft_session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if not draft_session:
            await ResponseHelper.send_error(interaction, "The draft session could not be found.")
            return
        
        confirm_view = CancelConfirmationView(
            self.bot, 
            self.draft_session_id, 
            interaction.user.display_name
        )
        
        await interaction.response.send_message(
            "Are you sure you want to cancel this draft?", 
            view=confirm_view, 
            ephemeral=True
        )

    async def remove_user_callback(self, interaction: discord.Interaction, button: Button):
        """Handle user removal request."""
        session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if not session:
            await ResponseHelper.send_error(interaction, "Draft session not found.")
            return
        
        # Check authorization
        if str(interaction.user.id) not in (session.sign_ups or {}):
            await ResponseHelper.send_error(
                interaction, 
                "You are not authorized to remove users."
            )
            return
        
        if session.sign_ups:
            options = [
                discord.SelectOption(label=user_name, value=user_id) 
                for user_id, user_name in session.sign_ups.items()
            ]
            view = UserRemovalView(session_id=session.session_id, options=options)
            await interaction.response.send_message(
                "Select a user to remove:", 
                view=view, 
                ephemeral=True
            )
        else:
            await ResponseHelper.send_error(interaction, "No users to remove.")

    # ========== Helper Methods ==========
    
    async def _check_weekly_limits(self, interaction: discord.Interaction, 
                                  user_id: str) -> bool:
        """Check if user has exceeded weekly draft limits."""
        pacific = pytz.timezone('US/Pacific')
        utc = pytz.utc
        now = datetime.now()
        pacific_time = utc.localize(now).astimezone(pacific)
        midnight_pacific = pacific.localize(
            datetime(pacific_time.year, pacific_time.month, pacific_time.day)
        )
        start_of_week = midnight_pacific - timedelta(days=midnight_pacific.weekday())
        
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                from session import PlayerLimit
                stmt = select(PlayerLimit).where(
                    PlayerLimit.player_id == user_id,
                    PlayerLimit.WeekStartDate == start_of_week
                )
                result = await db_session.execute(stmt)
                player_limit = result.scalars().first()
                
                if player_limit and player_limit.drafts_participated >= 4:
                    await ResponseHelper.send_error(
                        interaction,
                        "You have already participated in four drafts this week! "
                        "Next week begins Monday at midnight pacific time. "
                        "If you believe this is an error, please contact a Cube Overseer"
                    )
                    return False
        
        return True

    async def _check_should_ping(self, draft_session: DraftSession, 
                                player_count: int) -> bool:
        """Check if we should ping for more players."""
        now = datetime.now()
        ping_cooldown = draft_session.draft_start_time + timedelta(minutes=30)
        
        return (player_count in (5, 7) and 
                not draft_session.should_ping and 
                now > ping_cooldown)

    async def _send_draft_ping(self, interaction: discord.Interaction, 
                              draft_session: DraftSession, player_count: int):
        """Send a ping notification for more players."""
        config = get_config(interaction.guild_id)
        drafter_role_name = config["roles"]["drafter"]
        drafter_role = discord.utils.get(interaction.guild.roles, name=drafter_role_name)
        
        if drafter_role:
            channel = await interaction.client.fetch_channel(draft_session.draft_channel_id)
            if channel:
                await channel.send(f"{player_count} Players in queue! {drafter_role.mention}")

    async def _get_player_names(self, guild, player_ids: list) -> list:
        """Get display names for a list of player IDs."""
        names = []
        for pid in player_ids:
            member = guild.get_member(int(pid))
            if member:
                names.append(member.display_name)
        return names

    async def _calculate_stakes_for_teams(self, interaction: discord.Interaction, 
                                         draft_session: DraftSession):
        """Calculate and store stakes for staked draft teams."""
        # Get cap preferences
        all_players = draft_session.team_a + draft_session.team_b
        from preference_service import get_players_bet_capping_preferences
        cap_info = await get_players_bet_capping_preferences(
            all_players, 
            guild_id=str(interaction.guild_id)
        )
        
        # Calculate and store stakes
        await self._calculate_and_store_stakes(interaction, draft_session, cap_info)

    async def _calculate_and_store_stakes(self, interaction: discord.Interaction, 
                                        draft_session: DraftSession, 
                                        cap_info: dict = None):
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

    async def _notify_draft_manager(self, interaction: discord.Interaction):
        """Notify the draft manager about team creation."""
        try:
            manager = DraftSetupManager.get_active_manager(self.draft_session_id)
            if manager:
                logger.info(f"Notifying draft manager for session {self.draft_session_id}")
                manager.set_bot_instance(interaction.client)
                await manager.check_session_stage_and_organize()
                
                if manager.sio.connected:
                    await manager.sio.emit('getUsers')
        except Exception as e:
            logger.exception(f"Error notifying draft manager: {e}")

    # ========== Winston Draft Specific ==========
    
    async def start_draft_callback(self, interaction: discord.Interaction, button: Button):
        """Handle Winston draft start."""
        session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if not session:
            await ResponseHelper.send_error(interaction, "The draft session could not be found.")
            return
        
        if len(session.sign_ups) != 2:
            await ResponseHelper.send_error(
                interaction, 
                "Winston draft requires exactly 2 players."
            )
            return
        
        # Update session
        await DatabaseHelper.update_draft_session(
            self.draft_session_id,
            teams_start_time=datetime.now(),
            deletion_time=datetime.now() + timedelta(hours=4),
            session_stage='teams'
        )
        
        # Create teams
        await split_into_teams(interaction.client, self.draft_session_id)
        
        # Generate and send announcement
        await self._announce_winston_start(interaction)
        
        # Update button states
        ButtonStateManager.disable_all_except(self, ["cancel_draft"])
        
        await interaction.response.edit_message(
            embed=await self._create_winston_embed(session), 
            view=self
        )

    async def _handle_winston_signup_announcement(self, interaction: discord.Interaction,
                                                 draft_session: DraftSession,
                                                 sign_ups: dict):
        """Handle announcements for Winston draft signups."""
        if len(sign_ups) == 2:
            # Draft is ready
            sign_up_tags = ' '.join([f"<@{uid}>" for uid in sign_ups.keys()])
            channel = discord.utils.get(
                interaction.guild.text_channels, 
                name="winston-draft"
            )
            if channel:
                await channel.send(
                    f"Winston Draft Ready. Good luck in your match! {sign_up_tags}"
                )
        else:
            # Looking for opponent
            message_link = (
                f"https://discord.com/channels/{draft_session.guild_id}/"
                f"{draft_session.draft_channel_id}/{draft_session.message_id}"
            )
            channel = discord.utils.get(
                interaction.guild.text_channels, 
                name="cube-draft-open-play"
            )
            if channel:
                await channel.send(
                    f"**{interaction.user.display_name}** is looking for an opponent "
                    f"for a **Winston Draft**. [Join Here!]({message_link})"
                )

    # ========== Premade Draft Specific ==========
    
    async def team_assignment_callback(self, interaction: discord.Interaction, button: Button):
        """Handle team assignment for premade drafts."""
        session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if not session:
            await ResponseHelper.send_error(interaction, "The draft session could not be found.")
            return
        
        user_id = str(interaction.user.id)
        custom_id = button.custom_id
        
        # Determine which team
        if "Team_A" in custom_id:
            primary_team = session.team_a or []
            secondary_team = session.team_b or []
            primary_key = "team_a"
            secondary_key = "team_b"
            team_name = session.team_a_name
        elif "Team_B" in custom_id:
            primary_team = session.team_b or []
            secondary_team = session.team_a or []
            primary_key = "team_b"
            secondary_key = "team_a"
            team_name = session.team_b_name
        else:
            await ResponseHelper.send_error(
                interaction, 
                "An error occurred. Unable to determine the team."
            )
            return
        
        # Process assignment
        if user_id in primary_team:
            primary_team.remove(user_id)
            message = f"You have been removed from {team_name}."
        else:
            if user_id in secondary_team:
                secondary_team.remove(user_id)
            primary_team.append(user_id)
            message = f"You have been added to {team_name}."
        
        # Update sign-ups
        sign_ups = session.sign_ups or {}
        sign_ups[user_id] = interaction.user.display_name
        
        # Update database
        await DatabaseHelper.update_draft_session(
            self.draft_session_id,
            **{
                primary_key: primary_team,
                secondary_key: secondary_team,
                'sign_ups': sign_ups
            }
        )
        
        await ResponseHelper.send_success(interaction, message)
        await self.update_team_view(interaction)

    async def update_team_view(self, interaction: discord.Interaction):
        """Update the team view for premade drafts."""
        session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if not session:
            logger.error("Draft session not found")
            return
        
        channel = self.bot.get_channel(int(session.draft_channel_id))
        if not channel:
            logger.error(f"Channel not found: {session.draft_channel_id}")
            return
        
        try:
            message = await channel.fetch_message(int(session.message_id))
            embed = message.embeds[0]
            
            # Update team fields
            await self._update_team_fields(embed, session)
            
            await message.edit(embed=embed)
        except Exception as e:
            logger.error(f"Error updating team view: {e}")

    # ========== Test Mode Specific ==========

    async def add_test_users_callback(self, interaction: discord.Interaction, button: Button):
        """Add test users to the draft for testing purposes."""
        if not interaction.user.guild_permissions.administrator:
            await ResponseHelper.send_error(
                interaction, 
                "Only server administrators can use this test feature."
            )
            return
        
        await interaction.response.defer(ephemeral=True)
        
        draft_session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if not draft_session:
            await ResponseHelper.send_error(interaction, "The draft session could not be found.")
            return
        
        if draft_session.session_stage == "teams":
            await ResponseHelper.send_error(
                interaction, 
                "Cannot add test users after teams have been created."
            )
            return
        
        # Add test users
        result = await self._add_test_users_to_draft(draft_session)
        
        if result['added'] > 0:
            message = f"Added {result['added']} test users to the draft (total: {result['total']})."
            if draft_session.session_type == "staked":
                message += " Each user has different stake amounts and preferences."
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.followup.send(
                f"No additional test users were added. The draft already has "
                f"{result['total']} users (limit is {result['limit']}).", 
                ephemeral=True
            )
        
        # Update draft message
        await update_draft_message(interaction.client, self.draft_session_id)

    async def _add_test_users_to_draft(self, draft_session: DraftSession) -> dict:
        """Add test users to a draft session."""
        NUM_TEST_USERS_TO_ADD = 6
        start_id = 900000000000000000
        
        test_names = [
            "SuperLongUserName_Testing_Character_Limits_One",
            "AnotherVeryLongUsername_For_Testing_Two",
            "ThirdLongUsername_With_Extra_Characters_Three",
            "FourthLongUsername_To_Test_UI_Rendering_Four",
            "FifthLongUsername_With_Special_Chars_Five",
            "SixthLongUsername_Testing_Overflow_Six",
        ]
        
        sign_ups = draft_session.sign_ups or {}
        original_count = len(sign_ups)
        users_to_add = max(0, NUM_TEST_USERS_TO_ADD - original_count)
        users_to_add = min(users_to_add, len(test_names))
        
        if users_to_add <= 0:
            return {'added': 0, 'total': original_count, 'limit': NUM_TEST_USERS_TO_ADD}
        
        # Create test users
        fake_users = {}
        for i in range(users_to_add):
            user_id = str(start_id + i)
            fake_users[user_id] = test_names[i]
        
        sign_ups.update(fake_users)
        
        # Update database
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                await DatabaseHelper.update_draft_session(
                    self.draft_session_id,
                    sign_ups=sign_ups
                )
                
                # Add stake info for staked drafts
                if draft_session.session_type == "staked" and fake_users:
                    for user_id in fake_users:
                        stake_amount = random.randint(5, 20) * 10
                        stake_info = StakeInfo(
                            session_id=draft_session.session_id,
                            player_id=user_id,
                            max_stake=stake_amount,
                            assigned_stake=0,
                            is_capped=random.choice([True, False])
                        )
                        db_session.add(stake_info)
                
                await db_session.commit()
        
        return {'added': users_to_add, 'total': len(sign_ups), 'limit': NUM_TEST_USERS_TO_ADD}

    # ========== Class Methods ==========

    @classmethod
    async def create_rooms_pairings(cls, bot, guild, session_id: str, 
                                  interaction=None, session_type=None):
        """Create rooms and post pairings for a draft session."""
        # This is a large method that should be moved to a separate module
        # For now, keeping the same implementation but organizing it better
        from views.draft_rooms import create_rooms_and_pairings
        return await create_rooms_and_pairings(
            cls, bot, guild, session_id, interaction, session_type
        )

    async def _announce_teams(self, interaction: discord.Interaction):
        """Generate and send team announcement embeds."""
        session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if not session:
            return
        
        # Create announcement embeds
        embed, channel_embed = await self._create_team_embeds(session)
        
        # Create fresh view with properly set button states for all session types
        fresh_view = self._create_fresh_view_with_team_buttons(session)
        
        # Update the message with the new view
        try:
            await interaction.followup.edit_message(
                message_id=interaction.message.id, 
                embed=embed, 
                view=fresh_view
            )
        except Exception as e:
            logger.error(f"Failed to update draft message: {e}")
        
        # Send the channel announcement
        await interaction.channel.send(embed=channel_embed)

    def _create_fresh_view_with_team_buttons(self, session: DraftSession) -> discord.ui.View:
        """Create a fresh view with properly configured button states for the teams stage."""
        fresh_view = discord.ui.View(timeout=None)
        
        # Copy existing buttons with proper state management
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                button_copy = CallbackButton(
                    label=item.label,
                    style=item.style,
                    custom_id=item.custom_id,
                    custom_callback=item.custom_callback
                )
                
                # Set disabled state based on button type and session stage
                button_copy.disabled = self._should_button_be_disabled(item.custom_id, session)
                fresh_view.add_item(button_copy)
        
        # Add special buttons for staked sessions
        if session.session_type == "staked":
            fresh_view.add_item(StakeCalculationButton(session.session_id))
        
        return fresh_view

    def _should_button_be_disabled(self, custom_id: str, session: DraftSession) -> bool:
        """Determine if a button should be disabled based on session state."""
        # Buttons that should remain enabled after team creation
        enabled_buttons = [
            f"create_rooms_pairings_{self.draft_session_id}",
            f"cancel_draft_{self.draft_session_id}"
        ]
        
        # Special case: stake calculation button should be enabled for staked sessions
        if session.session_type == "staked" and "explain_stakes" in custom_id:
            return False
        
        # Enable rooms/pairings button (it was disabled initially)
        if custom_id == f"create_rooms_pairings_{self.draft_session_id}":
            return False
        
        # Keep cancel draft button enabled
        if custom_id == f"cancel_draft_{self.draft_session_id}":
            return False
        
        # Disable all other buttons after teams are created
        return True

    async def _create_team_embeds(self, session: DraftSession) -> tuple[discord.Embed, discord.Embed]:
        """Create team announcement embeds."""
        # Generate team display names and seating order
        if session.session_type != "swiss":
            sign_ups_list = list(session.sign_ups.keys())
            if session.session_type == "premade":
                seating_order = await generate_seating_order(self.bot, session)
            else:
                seating_order = [session.sign_ups[user_id] for user_id in sign_ups_list]
            
            team_a_display_names = [session.sign_ups[user_id] for user_id in session.team_a]
            team_b_display_names = [session.sign_ups[user_id] for user_id in session.team_b]
            random.shuffle(team_a_display_names)
            random.shuffle(team_b_display_names)
        else:
            sign_ups_list = list(session.sign_ups.keys())
            random.shuffle(sign_ups_list)
            seating_order = [session.sign_ups[user_id] for user_id in sign_ups_list]
            # Update database with new order for swiss
            new_sign_ups = {user_id: session.sign_ups[user_id] for user_id in sign_ups_list}
            async with AsyncSessionLocal() as db_session:
                async with db_session.begin():
                    await db_session.execute(
                        update(DraftSession)
                        .where(DraftSession.session_id == session.session_id)
                        .values(sign_ups=new_sign_ups)
                    )
                    await db_session.commit()
        
        # Create main embed
        embed = discord.Embed(
            title=f"Draft-{session.draft_id} is Ready!",
            description=(
                f"**Chosen Cube: [{session.cube}]"
                f"(https://cubecobra.com/cube/list/{session.cube})**\n\n" 
                "Host of Draftmancer must manually adjust seating as per below. \n"
                "**TURN OFF RANDOM SEATING SETTING IN DRAFTMANCER**\n\n"
                "**AFTER THE DRAFT**, select Create Chat Rooms and Post Pairings\n"
                "Pairings will post in the created draft-chat room"
            ),
            color=discord.Color.dark_gold() if session.session_type == "swiss" else discord.Color.blue()
        )
        
        # Add personalized draft links
        user_links = []
        for user_id, display_name in session.sign_ups.items():
            personalized_link = session.get_draft_link_for_user(display_name)
            user_links.append(f"**{display_name}**: [Draft Link]({personalized_link})")
        
        EmbedHelper.add_links_to_embed_safely(embed, user_links, "Your Personalized Draft Links")
        
        # Add team fields for non-swiss
        if session.session_type != 'swiss':
            team_a_name = "🔴 Team Red" if session.session_type in ["random", "staked"] else session.team_a_name
            team_b_name = "🔵 Team Blue" if session.session_type in ["random", "staked"] else session.team_b_name
            
            embed.add_field(name=team_a_name, value="\n".join(team_a_display_names), inline=True)
            embed.add_field(name=team_b_name, value="\n".join(team_b_display_names), inline=True)
        
        embed.add_field(name="Seating Order", value=" -> ".join(seating_order), inline=False)
        
        # Add stakes information for staked drafts
        if self.session_type == "staked":
            stake_lines, total_stakes = await get_formatted_stake_pairs(
                session.session_id,
                session.sign_ups
            )
            
            if stake_lines:
                # Format with bold names
                formatted_lines = []
                for line in stake_lines:
                    parts = line.split(': ')
                    names = parts[0].split(' vs ')
                    formatted_lines.append(f"**{names[0]}** vs **{names[1]}**: {parts[1]}")
                
                EmbedHelper.add_links_to_embed_safely(
                    embed, 
                    formatted_lines, 
                    f"Bets (Total: {total_stakes} tix)"
                )
        
        # Create channel embed
        channel_embed = discord.Embed(
            title="Teams have been formed. Seating Order Below!",
            description=f"**Chosen Cube: [{session.cube}]"
                       f"(https://cubecobra.com/cube/list/{session.cube})**\n\n",
            color=discord.Color.dark_gold() if session.session_type == "swiss" else discord.Color.green()
        )
        
        # Add team links to channel embed
        team_a_links = []
        team_b_links = []
        
        for user_id, display_name in session.sign_ups.items():
            personalized_link = session.get_draft_link_for_user(display_name)
            link_entry = f"**{display_name}**: [Draft Link]({personalized_link})"
            
            if session.session_type == 'swiss':
                team_a_links.append(link_entry)
            else:
                if user_id in session.team_a:
                    team_a_links.append(link_entry)
                elif user_id in session.team_b:
                    team_b_links.append(link_entry)
        
        # Add team links
        if team_a_links:
            team_name = "Team Red" if session.session_type in ["random", "staked"] else (session.team_a_name or "Team A")
            EmbedHelper.add_links_to_embed_safely(
                channel_embed, 
                team_a_links, 
                f"{team_name} Draft Links", 
                "red" if session.session_type in ["random", "staked"] else ""
            )
        
        if team_b_links:
            team_name = "Team Blue" if session.session_type in ["random", "staked"] else (session.team_b_name or "Team B")
            EmbedHelper.add_links_to_embed_safely(
                channel_embed, 
                team_b_links, 
                f"{team_name} Draft Links", 
                "blue" if session.session_type in ["random", "staked"] else ""
            )
        
        channel_embed.add_field(name="Seating Order", value=" -> ".join(seating_order), inline=False)
        
        return embed, channel_embed

    async def _handle_cube_update(self, select_interaction, draft_session: DraftSession):
        """Handle cube update selection."""
        try:
            cube_choice = select_interaction.data["values"][0]
            
            if draft_session.cube == cube_choice:
                await select_interaction.response.send_message(
                    f"The draft is already using the {cube_choice} cube.",
                    ephemeral=True
                )
                return
            
            await select_interaction.response.send_message(
                f"Updating draft to use cube: {cube_choice}...", 
                ephemeral=True
            )
            
            # Try to get existing manager
            manager = DraftSetupManager.get_active_manager(draft_session.session_id)
            
            if not manager:
                logger.warning(f"No active draft manager found for session {draft_session.session_id}")
                
                # Update database only
                async with AsyncSessionLocal() as db_session:
                    async with db_session.begin():
                        await db_session.execute(
                            update(DraftSession)
                            .where(DraftSession.session_id == draft_session.session_id)
                            .values(cube=cube_choice)
                        )
                        await db_session.commit()
                
                await update_draft_message(select_interaction.client, draft_session.session_id)
                
                await select_interaction.followup.send(
                    f"The draft has been updated to use cube: {cube_choice} "
                    f"(Note: Draft has already started, so the cube won't be updated in Draftmancer).",
                    ephemeral=True
                )
                return
            
            # Update cube through manager
            success = await manager.update_cube(cube_choice)
            
            if success:
                async with AsyncSessionLocal() as db_session:
                    async with db_session.begin():
                        await db_session.execute(
                            update(DraftSession)
                            .where(DraftSession.session_id == draft_session.session_id)
                            .values(cube=cube_choice)
                        )
                        await db_session.commit()
                
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

    async def _announce_winston_start(self, interaction: discord.Interaction):
        """Announce Winston draft start."""
        session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if not session:
            return
        
        channel_embed = await self._create_winston_channel_embed(session)
        await interaction.channel.send(embed=channel_embed)

    async def _create_winston_embed(self, session: DraftSession) -> discord.Embed:
        """Create Winston draft embed."""
        # Get display names
        sign_ups_list = list(session.sign_ups.keys())
        seating_order = [session.sign_ups[user_id] for user_id in sign_ups_list]
        team_a_display_names = [session.sign_ups[user_id] for user_id in session.team_a]
        team_b_display_names = [session.sign_ups[user_id] for user_id in session.team_b]
        
        embed = discord.Embed(
            title=f"Winston Draft-{session.draft_id} is Ready!",
            description=(
                f"**Chosen Cube: [{session.cube}]"
                f"(https://cubecobra.com/cube/list/{session.cube})**\n\n" 
                "Host of Draftmancer must manually adjust seating as per below."
            ),
            color=discord.Color.blue()
        )
        
        # Add personalized draft links
        user_links = []
        for user_id, display_name in session.sign_ups.items():
            personalized_link = session.get_draft_link_for_user(display_name)
            user_links.append(f"**{display_name}**: [Your Draft Link]({personalized_link})")
        
        embed.add_field(
            name="Your Personalized Draft Links",
            value="\n".join(user_links),
            inline=False
        )
        
        embed.add_field(name="🔴 Team Red", value="\n".join(team_a_display_names), inline=True)
        embed.add_field(name="🔵 Team Blue", value="\n".join(team_b_display_names), inline=True)
        embed.add_field(name="Seating Order", value=" -> ".join(seating_order), inline=False)
        
        return embed

    async def _create_winston_channel_embed(self, session: DraftSession) -> discord.Embed:
        """Create Winston draft channel announcement embed."""
        # Get display names
        sign_ups_list = list(session.sign_ups.keys())
        seating_order = [session.sign_ups[user_id] for user_id in sign_ups_list]
        
        embed = discord.Embed(
            title="Winston Draft Teams have been formed!",
            description=(
                f"**Chosen Cube: [{session.cube}]"
                f"(https://cubecobra.com/cube/list/{session.cube})**\n\n"
            ),
            color=discord.Color.green()
        )
        
        # Add personalized draft links
        user_links = []
        for user_id, display_name in session.sign_ups.items():
            personalized_link = session.get_draft_link_for_user(display_name)
            user_links.append(f"**{display_name}**: [Your Draft Link]({personalized_link})")
        
        embed.add_field(
            name="Your Personalized Draft Links",
            value="\n".join(user_links),
            inline=False
        )
        
        embed.add_field(name="Seating Order", value=" -> ".join(seating_order), inline=False)
        
        return embed

    async def _update_team_fields(self, embed: discord.Embed, session: DraftSession):
        """Update team fields in embed for premade drafts."""
        team_a_names = [
            session.sign_ups.get(str(user_id), "Unknown User") 
            for user_id in (session.team_a or [])
        ]
        team_b_names = [
            session.sign_ups.get(str(user_id), "Unknown User") 
            for user_id in (session.team_b or [])
        ]
        
        # Find team field indices
        team_a_index = None
        team_b_index = None
        
        for i, field in enumerate(embed.fields):
            if field.name.startswith(session.team_a_name or "Team A"):
                team_a_index = i
            elif field.name.startswith(session.team_b_name or "Team B"):
                team_b_index = i
        
        # Update team fields
        if team_a_index is not None:
            embed.set_field_at(
                team_a_index, 
                name=f"{session.team_a_name} ({len(session.team_a or [])}):", 
                value="\n".join(team_a_names) if team_a_names else "No players yet.", 
                inline=True
            )
        
        if team_b_index is not None:
            embed.set_field_at(
                team_b_index, 
                name=f"{session.team_b_name} ({len(session.team_b or [])}):", 
                value="\n".join(team_b_names) if team_b_names else "No players yet.", 
                inline=True
            )

    async def create_team_channel(self, guild, team_name: str, team_members, 
                                 team_a=None, team_b=None) -> int:
        """Create a team channel for the draft."""
        from config import get_config, is_special_guild

        config = get_config(guild.id)
        draft_category = discord.utils.get(guild.categories, name=config["categories"]["draft"])
        voice_category = None
        if is_special_guild(guild.id) and "voice" in config["categories"]:
            voice_category = discord.utils.get(guild.categories, name=config["categories"]["voice"])
        
        session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if not session:
            logger.error("Draft session not found")
            return
        
        channel_name = f"{team_name}-Chat-{session.draft_id}"

        # Get the admin role from config
        admin_role_name = config["roles"].get("admin")
        admin_role = discord.utils.get(guild.roles, name=admin_role_name) if admin_role_name else None
        
        # Basic permissions overwrites
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True)
        }

        # Add admin permissions for Draft chat only
        if team_name == "Draft" and admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(
                read_messages=True, 
                manage_messages=True
            )

        # Add team members with permissions
        for member in team_members:
            overwrites[member] = discord.PermissionOverwrite(
                read_messages=True, 
                manage_messages=True
            )
        
        # Create text channel
        channel = await guild.create_text_channel(
            name=channel_name, 
            overwrites=overwrites, 
            category=draft_category
        )
        self.channel_ids.append(channel.id)
        
        # Create voice channel for premade drafts
        if (session.premade_match_id and team_name != "Draft" and 
            session.session_type == "premade" and voice_category):
            voice_channel_name = f"{team_name}-Voice-{session.draft_id}"
            voice_channel = await guild.create_voice_channel(
                name=voice_channel_name, 
                overwrites=overwrites, 
                category=voice_category
            )
            self.channel_ids.append(voice_channel.id)

        # Set draft chat channel if this is the main channel
        if team_name == "Draft":
            self.draft_chat_channel = channel.id

        # Update database
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                update_values = {
                    'channel_ids': self.channel_ids,
                    'draft_chat_channel': self.draft_chat_channel,
                    'session_stage': 'pairings'
                }
                await db_session.execute(
                    update(DraftSession)
                    .where(DraftSession.session_id == self.draft_session_id)
                    .values(**update_values)
                )
                await db_session.commit()

        return channel.id