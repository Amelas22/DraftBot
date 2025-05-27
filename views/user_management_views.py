"""
User management related views for the draft bot.
"""

import discord
from discord.ui import View, Select
from discord import SelectOption
from typing import List
from loguru import logger
from sqlalchemy import update
from views.view_helpers import BaseView, ResponseHelper, DatabaseHelper
from session import AsyncSessionLocal, DraftSession

class UserRemovalView(BaseView):
    """View for removing users from a draft."""
    
    def __init__(self, session_id: str, options: List[SelectOption]):
        super().__init__(timeout=None)
        self.add_item(UserRemovalSelect(options=options, session_id=session_id))


class UserRemovalSelect(Select):
    """Select menu for choosing a user to remove."""
    
    def __init__(self, options: List[SelectOption], session_id: str, *args, **kwargs):
        super().__init__(
            *args, 
            **kwargs, 
            placeholder="Choose a user to remove...", 
            min_values=1, 
            max_values=1, 
            options=options
        )
        self.session_id = session_id

    async def callback(self, interaction: discord.Interaction):
        """Handle user removal selection."""
        await interaction.response.defer()
        bot = interaction.client
        
        # Get draft session
        session = await DatabaseHelper.get_draft_session_safe(self.session_id)
        if not session:
            await ResponseHelper.send_error(interaction, "Draft session not found.")
            return

        user_id_to_remove = self.values[0]
        
        if user_id_to_remove in session.sign_ups:
            removed_user_name = session.sign_ups.pop(user_id_to_remove)
            
            # Update database
            async with AsyncSessionLocal() as db_session:
                async with db_session.begin():
                    await db_session.execute(
                        update(DraftSession)
                        .where(DraftSession.session_id == session.session_id)
                        .values(sign_ups=session.sign_ups)
                    )
                    await db_session.commit()
                    
            # Update the draft message
            if session.session_type != "premade":
                from views.draft_message_utils import update_draft_message
                await update_draft_message(bot, session_id=session.session_id)
            else:
                # This is a circular dependency issue - we'll need to handle this
                # For now, we'll need to import this at runtime
                from .views import PersistentView
                await PersistentView.update_team_view(interaction)

            await interaction.followup.send(f"Removed {removed_user_name} from the draft.")
        else:
            await ResponseHelper.send_error(interaction, "User not found in sign-ups.")


class CancelConfirmationView(BaseView):
    """View for confirming draft cancellation."""
    
    def __init__(self, bot, draft_session_id: str, user_display_name: str):
        super().__init__(timeout=60)
        self.bot = bot
        self.draft_session_id = draft_session_id
        self.user_display_name = user_display_name

    @discord.ui.button(label="Yes, Cancel Draft", style=discord.ButtonStyle.danger)
    async def confirm_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Handle confirmation of draft cancellation."""
        from services.draft_setup_manager import DraftSetupManager, ACTIVE_MANAGERS
        
        # Disable all buttons
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        
        # Get session
        session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if not session:
            await interaction.followup.send("The draft session could not be found.", ephemeral=True)
            return
        
        # Announce cancellation
        channel = self.bot.get_channel(int(session.draft_channel_id))
        if channel:
            await channel.send(f"User **{self.user_display_name}** has cancelled the draft.")
        
        # Handle draft manager if exists
        manager = DraftSetupManager.get_active_manager(self.draft_session_id)
        if manager:
            logger.info(f"Found active draft manager for session {self.draft_session_id}, marking as cancelled")
            await manager.mark_draft_cancelled()
            await manager.disconnect_safely()
            
            if self.draft_session_id not in ACTIVE_MANAGERS:
                logger.success(f"Successfully removed manager for session {self.draft_session_id}")
            else:
                logger.warning(f"Failed to remove manager for session {self.draft_session_id}")
        
        # Delete the message
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

        await interaction.followup.send("The draft has been canceled.", ephemeral=True)

    @discord.ui.button(label="No, Keep Draft", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Handle cancellation of the cancellation."""
        # Disable all buttons
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Draft cancellation aborted.", view=self)