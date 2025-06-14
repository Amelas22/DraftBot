"""
Stake/betting related views and modals for the draft bot.
"""

import discord
from discord.ui import View, Button, Select, Modal
from typing import Optional, Dict, List
from datetime import datetime, timedelta
from loguru import logger
from sqlalchemy import select, update, and_
from sqlalchemy.orm import selectinload

from views.view_helpers import (
    BaseView, BaseModal, CallbackButton, DatabaseHelper, 
    ResponseHelper, EmbedHelper, PermissionHelper
)
from session import AsyncSessionLocal, DraftSession, StakeInfo, get_draft_session
from config import get_config
from draft_organization.stake_calculator import calculate_stakes_with_strategy
from utils import get_formatted_stake_pairs, get_missing_stake_players


class StakeOptionsView(BaseView):
    """View for selecting stake options when signing up for a staked draft."""
    
    def __init__(self, draft_session_id: str, draft_link: str, user_display_name: str, 
                 min_stake: int, has_draftmancer_role: bool = False):
        super().__init__(timeout=300)  # 5 minute timeout
        self.add_item(StakeOptionsSelect(
            draft_session_id, 
            draft_link, 
            user_display_name, 
            min_stake,
            has_draftmancer_role
        ))


class StakeOptionsSelect(Select):
    """Select menu for choosing stake amounts."""
    
    def __init__(self, draft_session_id: str, draft_link: str, user_display_name: str, 
                 min_stake: int, has_draftmancer_role: bool = False):
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

        super().__init__(
            placeholder=f"Select your maximum bet... ", 
            min_values=1, 
            max_values=1, 
            options=options
        )
        
    async def callback(self, interaction: discord.Interaction):
        """Handle stake selection."""
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
            
            # Set the default value based on saved preference
            stake_modal.default_cap_setting = is_capped
            stake_modal.cap_checkbox.value = "yes" if is_capped else "no"
            
            await interaction.response.send_modal(stake_modal)
        else:
            # Process the selected preset stake amount
            stake_amount = int(selected_value)
            await self.handle_stake_submission(interaction, stake_amount, is_capped=is_capped)
            
    async def handle_stake_submission(self, interaction: discord.Interaction, 
                                    stake_amount: int, is_capped: bool = True):
        """Handle the submission of a stake amount."""
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)
        display_name = str(interaction.user.display_name)
        
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Get the draft session
                draft_session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
                if not draft_session:
                    await ResponseHelper.send_error(interaction, "Draft session not found.")
                    return
                
                # Update sign_ups
                sign_ups = draft_session.sign_ups or {}
                sign_ups[user_id] = interaction.user.display_name
                
                # Update draftmancer_role_users if user has the role
                draftmancer_role_users = draft_session.draftmancer_role_users or []
                if self.has_draftmancer_role and display_name not in draftmancer_role_users:
                    draftmancer_role_users.append(display_name)
                
                # Check if we should ping
                should_ping = False
                now = datetime.now()
                ping_cooldown = draft_session.draft_start_time + timedelta(minutes=30)
                
                if len(sign_ups) in (5, 7) and not draft_session.should_ping and now > ping_cooldown:
                    should_ping = True
                
                # Update draft session
                values_to_update = {
                    "sign_ups": sign_ups,
                    "draftmancer_role_users": draftmancer_role_users
                }
                if should_ping:
                    values_to_update["should_ping"] = True

                # Reset the inactivity timer when a user signs up
                if not draft_session.session_stage:
                    values_to_update["deletion_time"] = datetime.now() + timedelta(minutes=180)

                await DatabaseHelper.update_draft_session(self.draft_session_id, **values_to_update)
                
                # Check if stake record exists
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
        
        # Send ping if needed
        if should_ping:
            await self._send_ping_notification(interaction, len(sign_ups))
        
        # Confirm stake and provide draft link
        cap_status = "capped at the highest opponent bet" if is_capped else "NOT capped (full action)"
        
        draft_session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        personalized_link = draft_session.get_draft_link_for_user(display_name) if draft_session else self.draft_link
        
        signup_message = (
            f"You've set your maximum stake to {stake_amount} tix.\n"
            f"Your bet will be {cap_status}.\n\n"
            f"You are now signed up. Join Here: {personalized_link}"
        )
        
        await ResponseHelper.send_success(interaction, signup_message)
        
        # Update the draft message
        from .views import update_draft_message
        await update_draft_message(interaction.client, self.draft_session_id)
    
    async def _send_ping_notification(self, interaction: discord.Interaction, player_count: int):
        """Send a ping notification when player count reaches threshold."""
        config = get_config(interaction.guild_id)
        drafter_role_name = config["roles"]["drafter"]
        
        guild = interaction.guild
        drafter_role = discord.utils.get(guild.roles, name=drafter_role_name)
        
        if drafter_role:
            # Get the draft session to find the channel
            draft_session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
            if draft_session:
                channel = await interaction.client.fetch_channel(draft_session.draft_channel_id)
                if channel:
                    await channel.send(f"{player_count} Players in queue! {drafter_role.mention}")


class StakeModal(BaseModal):
    """Modal for entering custom stake amounts."""
    
    def __init__(self, over_100: bool = False):
        super().__init__(title="Enter Maximum Bet")
        
        self.over_100 = over_100
        self.default_cap_setting = True  
        self.has_draftmancer_role = False
        
        placeholder_text = (
            "Reminder: Your bet can fill multiple bets when possible" 
            if over_100 else "Enter maximum amount you're willing to bet"
        )
        
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
                value="yes"
            )
            self.add_item(self.cap_checkbox)
        
        # These will be set separately before sending the modal
        self.draft_session_id = None
        self.draft_link = None
        self.user_display_name = None

    async def callback(self, interaction: discord.Interaction):
        """Handle modal submission."""
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)
        
        try:
            # Parse the stake amount
            max_stake = int(self.stake_input.value)
        except ValueError:
            await ResponseHelper.send_error(interaction, "Please enter a valid number.")
            return
        
        # Determine if bet should be capped
        is_capped = True  # Default for regular stakes
        if self.over_100:
            cap_value = self.cap_checkbox.value.lower()
            is_capped = cap_value in ('yes', 'y', 'true')
        
        # Validation for over 100 stakes
        if self.over_100:
            if max_stake <= 100:
                await ResponseHelper.send_error(interaction, "Amount must be greater than 100 tix.")
                return
            if max_stake % 50 != 0:
                await ResponseHelper.send_error(
                    interaction, 
                    "Amount must be a multiple of 50 (e.g., 150, 200, 250)."
                )
                return
        
        # Process the stake submission
        await self._process_stake_submission(interaction, user_id, guild_id, max_stake, is_capped)
    
    async def _process_stake_submission(self, interaction: discord.Interaction, 
                                      user_id: str, guild_id: str, max_stake: int, 
                                      is_capped: bool):
        """Process the stake submission."""
        # Update the player's preference
        from preference_service import update_player_bet_capping_preference
        await update_player_bet_capping_preference(user_id, guild_id, is_capped)
        
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Check if draft_session_id is set
                if not self.draft_session_id:
                    await ResponseHelper.send_error(
                        interaction, 
                        "Error: Draft session ID is missing. Please try again."
                    )
                    return
                
                # Get the draft session
                draft_session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
                if not draft_session:
                    await ResponseHelper.send_error(interaction, "Draft session not found.")
                    return
                
                # Validate against minimum stake
                min_stake = draft_session.min_stake or 10
                if max_stake < min_stake:
                    await ResponseHelper.send_error(
                        interaction, 
                        f"Minimum stake for this draft is {min_stake} tix."
                    )
                    return
                
                # Update sign_ups
                sign_ups = draft_session.sign_ups or {}
                display_name = self.user_display_name or interaction.user.display_name
                sign_ups[user_id] = display_name
                
                # Update draftmancer_role_users
                draftmancer_role_users = draft_session.draftmancer_role_users or []
                if self.has_draftmancer_role and display_name not in draftmancer_role_users:
                    draftmancer_role_users.append(display_name)
                
                # Update the draft session
                await DatabaseHelper.update_draft_session(
                    self.draft_session_id,
                    sign_ups=sign_ups,
                    draftmancer_role_users=draftmancer_role_users
                )
                
                # Handle stake record
                stake_stmt = select(StakeInfo).where(and_(
                    StakeInfo.session_id == self.draft_session_id,
                    StakeInfo.player_id == user_id
                ))
                stake_result = await session.execute(stake_stmt)
                stake_info = stake_result.scalars().first()
                
                if stake_info:
                    stake_info.max_stake = max_stake
                    stake_info.is_capped = is_capped
                else:
                    stake_info = StakeInfo(
                        session_id=self.draft_session_id,
                        player_id=user_id,
                        max_stake=max_stake,
                        is_capped=is_capped
                    )
                    session.add(stake_info)
                
                await session.commit()
        
        # Prepare response message
        cap_status = "capped at the highest opponent bet" if is_capped else "NOT capped (full action)"
        signup_message = (
            f"You've set your maximum stake to {max_stake} tix.\n"
            f"Your bet will be {cap_status}.\n\n"
            f"This setting will be remembered for future drafts."
        )
        
        if max_stake > 100:
            signup_message += (
                "\n\nReminder: Your max bet will be used to fill as many "
                "opposing team bets as possible."
            )
        
        if self.draft_link:
            draft_session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
            personalized_link = (
                draft_session.get_draft_link_for_user(interaction.user.display_name) 
                if draft_session else self.draft_link
            )
            signup_message += f"\n\nYou are now signed up. Join Here: {personalized_link}"
        
        await ResponseHelper.send_success(interaction, signup_message)
        
        # Update the draft message
        from .views import update_draft_message
        await update_draft_message(interaction.client, self.draft_session_id)


class StakeCalculationButton(Button):
    """Button to show how stakes were calculated."""
    
    def __init__(self, session_id: str):
        super().__init__(
            label="How Bets Were Calculated",
            style=discord.ButtonStyle.green,
            custom_id=f"stake_calculation_{session_id}"
        )
        self.session_id = session_id
        
    async def callback(self, interaction: discord.Interaction):
        """Show detailed stake calculation explanation."""
        await interaction.response.defer(ephemeral=True)
        
        # Get the draft session
        draft_session = await DatabaseHelper.get_draft_session_safe(self.session_id)
        if not draft_session:
            await ResponseHelper.send_error(interaction, "Draft session not found.")
            return
        
        try:
            # Generate explanation embeds
            embeds = await self._generate_stake_explanation(draft_session)
            
            # Create the paginated view
            view = PaginatedStakeExplanation(embeds)
            
            # Send the first embed with the view
            await interaction.followup.send(embed=embeds[0], view=view, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error generating stake explanation: {e}", exc_info=True)
            await ResponseHelper.send_error(
                interaction, 
                f"Error generating explanation: {str(e)}"
            )
    
    async def _generate_stake_explanation(self, draft_session: DraftSession) -> List[discord.Embed]:
        """Generate detailed stake calculation explanation embeds."""
        # This is a simplified version - you would need to implement the full logic
        # based on the original generate_explanation method
        
        # Fetch stake information
        stake_data = await self._fetch_stake_data(draft_session)
        
        # Generate embeds
        embeds = []
        
        # Page 1: Core Principles
        embed1 = discord.Embed(
            title="Dynamic Bet System: Overview",
            color=discord.Color.green()
        )
        embed1.add_field(
            name="Core Principles",
            value=(
                "• Players never bet more than their maximum specified amount\n"
                "• Teams were created randomly FIRST, then bets were allocated"
            ),
            inline=False
        )
        embeds.append(embed1)
        
        # Additional pages would be added here...
        
        return embeds
    
    async def _fetch_stake_data(self, draft_session: DraftSession) -> Dict:
        """Fetch stake data for the draft session."""
        async with AsyncSessionLocal() as session:
            stake_stmt = select(StakeInfo).where(StakeInfo.session_id == self.session_id)
            results = await session.execute(stake_stmt)
            stake_infos = results.scalars().all()
            
            # Build stake data dictionary
            stake_data = {
                'max_stakes': {},
                'cap_info': {},
                'stake_infos': stake_infos
            }
            
            for info in stake_infos:
                stake_data['max_stakes'][info.player_id] = info.max_stake
                stake_data['cap_info'][info.player_id] = info.is_capped
            
            return stake_data


class PaginatedStakeExplanation(BaseView):
    """View for paginated stake explanation."""
    
    def __init__(self, embeds: List[discord.Embed], timeout: int = 180):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.current_page = 0
        self.update_button_states()
    
    def update_button_states(self):
        """Update button states based on current page."""
        # Disable previous button if on first page
        self.children[0].disabled = (self.current_page == 0)
        # Disable next button if on last page
        self.children[2].disabled = (self.current_page == len(self.embeds) - 1)
        # Update page counter
        self.children[1].label = f"Page {self.current_page + 1}/{len(self.embeds)}"
    
    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_button(self, button: Button, interaction: discord.Interaction):
        """Go to previous page."""
        if self.current_page > 0:
            self.current_page -= 1
            self.update_button_states()
            await interaction.response.edit_message(
                embed=self.embeds[self.current_page], 
                view=self
            )
    
    @discord.ui.button(label="Page 1/1", style=discord.ButtonStyle.gray, disabled=True)
    async def page_counter(self, button: Button, interaction: discord.Interaction):
        """Page counter button (not clickable)."""
        await interaction.response.defer()
    
    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_button(self, button: Button, interaction: discord.Interaction):
        """Go to next page."""
        if self.current_page < len(self.embeds) - 1:
            self.current_page += 1
            self.update_button_states()
            await interaction.response.edit_message(
                embed=self.embeds[self.current_page], 
                view=self
            )


class BetCapToggleButton(CallbackButton):
    """Button for toggling bet cap settings."""
    
    def __init__(self, draft_session_id: str):
        super().__init__(
            label="Change Bet/Settings",
            style=discord.ButtonStyle.secondary,
            custom_id=f"bet_cap_toggle_{draft_session_id}",
            custom_callback=self.bet_cap_callback
        )
        self.draft_session_id = draft_session_id
    
    async def bet_cap_callback(self, interaction: discord.Interaction, button: Button):
        """Handle bet cap toggle callback."""
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)
        
        # Check if user is in the draft
        is_in_draft, draft_session = await PermissionHelper.check_user_in_draft(
            interaction, self.draft_session_id
        )
        
        if not draft_session:
            await ResponseHelper.send_error(interaction, "Draft session not found.")
            return
        
        if not is_in_draft:
            await ResponseHelper.send_error(interaction, "You're not registered for this draft.")
            return
        
        # Get the user's stake info
        stake_info = await self._get_user_stake_info(user_id)
        if not stake_info:
            await ResponseHelper.send_error(interaction, "You need to set a stake amount first.")
            return
        
        # Create the combined view
        view = await self._create_combined_view(
            interaction, draft_session, stake_info, user_id, guild_id
        )
        
        # Send the message
        message_content = (
            f"Your current bet is {stake_info.max_stake} tix with bet cap "
            f"{'ON 🧢' if stake_info.is_capped else 'OFF 🏎️'}.\n"
            f"Min Bet for queue is {draft_session.min_stake or 10}. "
            f"Select a new max bet and/or adjust your cap settings.\n"
            f"Your bet cap preferences will be saved for future drafts."
        )
        
        await interaction.response.send_message(
            content=message_content,
            view=view,
            ephemeral=True
        )
    
    async def _get_user_stake_info(self, user_id: str) -> Optional[StakeInfo]:
        """Get stake info for a user."""
        async with AsyncSessionLocal() as session:
            stake_stmt = select(StakeInfo).where(and_(
                StakeInfo.session_id == self.draft_session_id,
                StakeInfo.player_id == user_id
            ))
            result = await session.execute(stake_stmt)
            return result.scalars().first()
    
    async def _create_combined_view(self, interaction: discord.Interaction, 
                                  draft_session: DraftSession, stake_info: StakeInfo,
                                  user_id: str, guild_id: str) -> View:
        """Create the combined view with stake options and cap settings."""
        view = View(timeout=None)
        
        # Add stake options dropdown
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
        
        stake_select = CombinedStakeSelect(
            draft_session_id=self.draft_session_id,
            draft_link=draft_session.draft_link,
            user_display_name=interaction.user.display_name,
            min_stake=min_stake,
            current_stake=stake_info.max_stake,
            options=options
        )
        view.add_item(stake_select)
        
        # Add bet cap status button
        status = "ON 🧢" if stake_info.is_capped else "OFF 🏎️"
        style = discord.ButtonStyle.green if stake_info.is_capped else discord.ButtonStyle.red
        
        status_button = Button(
            label=f"Bet Cap: {status}",
            style=style,
            custom_id=f"bet_cap_status_{self.draft_session_id}",
            disabled=True
        )
        view.add_item(status_button)
        
        # Add ON/OFF buttons
        yes_button = Button(
            label="Turn ON 🧢",
            style=discord.ButtonStyle.green,
            custom_id=f"cap_yes_{self.draft_session_id}"
        )
        yes_button.callback = lambda i: self._update_cap_status(i, user_id, guild_id, True)
        view.add_item(yes_button)
        
        no_button = Button(
            label="Turn OFF 🏎️", 
            style=discord.ButtonStyle.red,
            custom_id=f"cap_no_{self.draft_session_id}"
        )
        no_button.callback = lambda i: self._update_cap_status(i, user_id, guild_id, False)
        view.add_item(no_button)
        
        return view
    
    async def _update_cap_status(self, interaction: discord.Interaction, 
                               user_id: str, guild_id: str, is_capped: bool):
        """Update cap status for a user."""
        # Check if interaction is from the correct user
        if str(interaction.user.id) != user_id:
            await ResponseHelper.send_error(interaction, "This button is not for you.")
            return
        
        # Update stake info
        async with AsyncSessionLocal() as session:
            async with session.begin():
                stake_stmt = select(StakeInfo).where(and_(
                    StakeInfo.session_id == self.draft_session_id,
                    StakeInfo.player_id == user_id
                ))
                result = await session.execute(stake_stmt)
                stake_info = result.scalars().first()
                
                if not stake_info:
                    await ResponseHelper.send_error(interaction, "Error: Stake info not found.")
                    return
                
                stake_info.is_capped = is_capped
                session.add(stake_info)
                await session.commit()
        
        # Update preference for future drafts
        from preference_service import update_player_bet_capping_preference
        await update_player_bet_capping_preference(user_id, guild_id, is_capped)
        
        # Update the draft message
        from .views import update_draft_message
        await update_draft_message(interaction.client, self.draft_session_id)
        
        # Send confirmation
        status_text = "ON 🧢" if is_capped else "OFF 🏎️"
        description = (
            "capped at the highest opponent bet" if is_capped else 
            "NOT capped and may be spread across multiple opponents"
        )
        
        await ResponseHelper.send_success(
            interaction,
            f"Your bet cap has been turned {status_text}. Your bet will be {description}.\n\n"
            f"This preference will be remembered for future drafts."
        )


class CombinedStakeSelect(Select):
    """Select menu for changing stake amount in combined view."""
    
    def __init__(self, draft_session_id: str, draft_link: str, user_display_name: str,
                 min_stake: int, current_stake: int, options: List[discord.SelectOption]):
        self.draft_session_id = draft_session_id
        self.draft_link = draft_link
        self.user_display_name = user_display_name
        self.min_stake = min_stake
        self.current_stake = current_stake
        
        placeholder = f"Current Bet: {current_stake} tix - Select new max bet..."
        
        super().__init__(
            placeholder=placeholder, 
            min_values=1, 
            max_values=1, 
            options=options
        )
        
    async def callback(self, interaction: discord.Interaction):
        """Handle stake selection in combined view."""
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild_id)
        
        # Load the user's saved preference
        from preference_service import get_player_bet_capping_preference
        is_capped = await get_player_bet_capping_preference(user_id, guild_id)
        
        selected_value = self.values[0]
        
        if selected_value == "over_100":
            # Create modal for custom amount
            stake_modal = StakeModal(over_100=True)
            stake_modal.draft_session_id = self.draft_session_id
            stake_modal.draft_link = self.draft_link
            stake_modal.user_display_name = self.user_display_name
            
            # Set the default value based on saved preference
            stake_modal.default_cap_setting = is_capped
            stake_modal.cap_checkbox.value = "yes" if is_capped else "no"
            
            await interaction.response.send_modal(stake_modal)
        else:
            # Process the selected preset stake amount
            stake_amount = int(selected_value)
            
            # Only proceed if different from current stake
            if stake_amount == self.current_stake:
                await ResponseHelper.send_error(
                    interaction, 
                    f"You already have a {stake_amount} tix stake set."
                )
                return
            
            # Handle stake submission
            await self._handle_stake_update(interaction, stake_amount, is_capped)
    
    async def _handle_stake_update(self, interaction: discord.Interaction, 
                                 stake_amount: int, is_capped: bool):
        """Handle updating an existing stake."""
        user_id = str(interaction.user.id)
        
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Get the draft session
                draft_session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
                if not draft_session:
                    await ResponseHelper.send_error(interaction, "Draft session not found.")
                    return
                
                # Ensure user is in sign_ups
                sign_ups = draft_session.sign_ups or {}
                sign_ups[user_id] = interaction.user.display_name
                
                await DatabaseHelper.update_draft_session(
                    self.draft_session_id,
                    sign_ups=sign_ups
                )
                
                # Update stake record
                stake_stmt = select(StakeInfo).where(and_(
                    StakeInfo.session_id == self.draft_session_id,
                    StakeInfo.player_id == user_id
                ))
                stake_result = await session.execute(stake_stmt)
                stake_info = stake_result.scalars().first()
                
                if stake_info:
                    stake_info.max_stake = stake_amount
                    stake_info.is_capped = is_capped
                else:
                    stake_info = StakeInfo(
                        session_id=self.draft_session_id,
                        player_id=user_id,
                        max_stake=stake_amount,
                        is_capped=is_capped
                    )
                    session.add(stake_info)
                
                await session.commit()
        
        # Send confirmation
        cap_status = "capped at the highest opponent bet" if is_capped else "NOT capped (full action)"
        message = (
            f"You've updated your maximum bet to {stake_amount} tix.\n"
            f"Your bet will be {cap_status}."
        )
        
        await ResponseHelper.send_success(interaction, message)
        
        # Update the draft message
        from .views import update_draft_message
        await update_draft_message(interaction.client, self.draft_session_id)