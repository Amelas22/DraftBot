"""
Ready check functionality for the draft bot.
"""

import discord
from discord.ui import View, Button
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import asyncio
from loguru import logger

from views.view_helpers import (
    BaseView, ResponseHelper, EmbedHelper, DatabaseHelper,
    CooldownManager, ProcessingLockManager
)
from session import get_draft_session, DraftSession
from utils import get_missing_stake_players


# Global ready check sessions storage
READY_CHECK_SESSIONS: Dict[str, Dict[str, List[str]]] = {}
READY_CHECK_COOLDOWNS: Dict[str, datetime] = {}


class ReadyCheckView(BaseView):
    """View for ready check buttons."""
    
    def __init__(self, draft_session_id: str):
        super().__init__(timeout=None)
        self.draft_session_id = draft_session_id
        
        # Create buttons with unique custom IDs
        self.ready_button.custom_id = f"ready_check_ready_{self.draft_session_id}"
        self.not_ready_button.custom_id = f"ready_check_not_ready_{self.draft_session_id}"

    @discord.ui.button(label="Ready", style=discord.ButtonStyle.green, custom_id="placeholder_ready")
    async def ready_button(self, button: Button, interaction: discord.Interaction):
        """Handle ready button click."""
        await self.handle_ready_not_ready_interaction(interaction, "ready")

    @discord.ui.button(label="Not Ready", style=discord.ButtonStyle.red, custom_id="placeholder_not_ready")
    async def not_ready_button(self, button: Button, interaction: discord.Interaction):
        """Handle not ready button click."""
        await self.handle_ready_not_ready_interaction(interaction, "not_ready")

    async def handle_ready_not_ready_interaction(self, interaction: discord.Interaction, status: str):
        """Handle ready/not ready interaction."""
        session = READY_CHECK_SESSIONS.get(self.draft_session_id)
        if not session:
            await ResponseHelper.send_error(interaction, "Session data is missing.")
            return

        user_id = str(interaction.user.id)
        
        # Check if user is authorized
        if not self._is_user_authorized(user_id, session):
            await ResponseHelper.send_error(
                interaction, 
                "You are not authorized to interact with this button."
            )
            return

        # Update the ready check status
        self._update_user_status(user_id, status, session)

        # Get draft session and generate updated embed
        draft_session = await DatabaseHelper.get_draft_session_safe(self.draft_session_id)
        if not draft_session:
            await ResponseHelper.send_error(interaction, "Draft session not found.")
            return
            
        embed = await generate_ready_check_embed(
            session, draft_session.sign_ups, draft_session.draft_link, draft_session
        )

        # Update the message
        await interaction.response.edit_message(embed=embed, view=self)
    
    def _is_user_authorized(self, user_id: str, session: Dict[str, List[str]]) -> bool:
        """Check if user is authorized to interact with ready check."""
        return any(user_id in session[state] for state in ['ready', 'not_ready', 'no_response'])
    
    def _update_user_status(self, user_id: str, new_status: str, session: Dict[str, List[str]]):
        """Update user's ready check status."""
        # Remove user from all states
        for state in ['ready', 'not_ready', 'no_response']:
            if user_id in session[state]:
                session[state].remove(user_id)
        
        # Add user to new state
        session[new_status].append(user_id)


async def generate_ready_check_embed(ready_check_status: Dict[str, List[str]], 
                                   sign_ups: Dict[str, str], 
                                   draft_link: str, 
                                   draft_session: Optional[DraftSession] = None) -> discord.Embed:
    """Generate the ready check embed with current status."""
    def get_names(user_ids: List[str]) -> str:
        return "\n".join(sign_ups.get(user_id, "Unknown user") for user_id in user_ids) or "None"

    # Create the embed
    embed = discord.Embed(
        title="Ready Check Initiated", 
        description="Please indicate if you are ready.", 
        color=discord.Color.gold()
    )
    
    # Add status fields
    embed.add_field(name="Ready", value=get_names(ready_check_status['ready']), inline=False)
    embed.add_field(name="Not Ready", value=get_names(ready_check_status['not_ready']), inline=False)
    embed.add_field(name="No Response", value=get_names(ready_check_status['no_response']), inline=False)
    
    # Add draft links
    if draft_session:
        user_links = []
        for user_id, display_name in sign_ups.items():
            personalized_link = draft_session.get_draft_link_for_user(display_name)
            user_links.append(f"**{display_name}**: [Draft Link]({personalized_link})")
        
        EmbedHelper.add_links_to_embed_safely(embed, user_links, "Your Personalized Draft Links")
    else:
        # Fallback for backwards compatibility
        embed.add_field(
            name="Draftmancer Link", 
            value=f"**➡️ [JOIN DRAFT HERE]({draft_link})⬅️**", 
            inline=False
        )
    
    return embed


class ReadyCheckManager:
    """Manager for ready check functionality."""
    
    @staticmethod
    async def initiate_ready_check(interaction: discord.Interaction, 
                                  draft_session_id: str, 
                                  view_instance) -> bool:
        """
        Initiate a ready check for a draft session.
        
        Args:
            interaction: The Discord interaction
            draft_session_id: The draft session ID
            view_instance: The PersistentView instance
            
        Returns:
            bool: True if ready check was initiated, False otherwise
        """
        # Check cooldown
        if not await CooldownManager.check_cooldown(
            READY_CHECK_COOLDOWNS, draft_session_id, 60, interaction
        ):
            return False
        
        # Get session
        session = await DatabaseHelper.get_draft_session_safe(draft_session_id)
        if not session:
            await ResponseHelper.send_error(interaction, "The draft session could not be found.")
            return False
        
        # Validate player count
        sign_up_count = len(session.sign_ups)
        if sign_up_count not in (6, 8, 10):
            await ResponseHelper.send_error(
                interaction,
                f"Ready check only available with 6, 8, or 10 players. "
                f"Currently {sign_up_count} players in queue."
            )
            return False
        
        # Check if user is in the session
        user_id = str(interaction.user.id)
        if user_id not in session.sign_ups:
            await ResponseHelper.send_error(
                interaction, 
                "You are not registered in the draft session."
            )
            return False
        
        # For staked drafts, check if all players have set their stakes
        if session.session_type == "staked":
            missing_players = await get_missing_stake_players(draft_session_id)
            if missing_players:
                # Get display names
                guild = interaction.guild
                missing_names = []
                for pid in missing_players:
                    member = guild.get_member(int(pid))
                    if member:
                        missing_names.append(member.display_name)
                
                players_str = ", ".join(missing_names)
                await ResponseHelper.send_error(
                    interaction,
                    f"Cannot initiate ready check yet. The following players need to set "
                    f"their stakes: {players_str}"
                )
                return False
        
        # Create ready check status
        ready_check_status = {
            "ready": [user_id],  # Initiator is automatically ready
            "not_ready": [],
            "no_response": [uid for uid in session.sign_ups.keys() if uid != user_id]
        }
        
        # Store in global sessions
        READY_CHECK_SESSIONS[draft_session_id] = ready_check_status
        logger.info(f"✅ Ready check initiated for session {draft_session_id}")
        
        # Disable the ready check button on the view
        if view_instance:
            for item in view_instance.children:
                if isinstance(item, Button) and item.custom_id.endswith("ready_check"):
                    item.disabled = True
                    break
        
        # Generate embed and create view
        embed = await generate_ready_check_embed(
            ready_check_status, session.sign_ups, session.draft_link, session
        )
        view = ReadyCheckView(draft_session_id)
        
        # Send the ready check message
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        
        # Send mention message
        user_mentions = ' '.join([f"<@{user_id}>" for user_id in session.sign_ups.keys()])
        await interaction.followup.send(f"Ready Check Initiated {user_mentions}", ephemeral=False)
        
        return True
    
    @staticmethod
    def get_ready_check_status(draft_session_id: str) -> Optional[Dict[str, List[str]]]:
        """Get the ready check status for a draft session."""
        return READY_CHECK_SESSIONS.get(draft_session_id)
    
    @staticmethod
    def clear_ready_check(draft_session_id: str):
        """Clear the ready check data for a draft session."""
        if draft_session_id in READY_CHECK_SESSIONS:
            del READY_CHECK_SESSIONS[draft_session_id]
            logger.info(f"Cleared ready check data for session {draft_session_id}")
    
    @staticmethod
    def is_ready_check_complete(draft_session_id: str) -> bool:
        """Check if all players have responded to the ready check."""
        status = READY_CHECK_SESSIONS.get(draft_session_id)
        if not status:
            return False
        
        # Check if no_response list is empty
        return len(status.get('no_response', [])) == 0
    
    @staticmethod
    def all_players_ready(draft_session_id: str) -> bool:
        """Check if all players are ready."""
        status = READY_CHECK_SESSIONS.get(draft_session_id)
        if not status:
            return False
        
        # Check if all players are in the ready list
        return (len(status.get('not_ready', [])) == 0 and 
                len(status.get('no_response', [])) == 0)