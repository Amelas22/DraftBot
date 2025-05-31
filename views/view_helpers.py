"""
Helper functions and base classes for Discord views.
This module contains common functionality shared across different view types.
"""

import discord
from discord.ui import View, Button, Select
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime, timedelta
import asyncio
from loguru import logger
from sqlalchemy import select, update
from session import AsyncSessionLocal, DraftSession, StakeInfo, get_draft_session
from config import get_config


class DatabaseHelper:
    """Helper class for common database operations."""
    
    @staticmethod
    async def update_draft_session(session_id: str, **kwargs) -> bool:
        """
        Update a draft session with the given parameters.
        
        Args:
            session_id: The draft session ID
            **kwargs: Fields to update
            
        Returns:
            bool: Success status
        """
        try:
            async with AsyncSessionLocal() as db_session:
                async with db_session.begin():
                    result = await db_session.execute(
                        update(DraftSession)
                        .where(DraftSession.session_id == session_id)
                        .values(**kwargs)
                    )
                    await db_session.commit()
                    return result.rowcount > 0
        except Exception as e:
            logger.error(f"Error updating draft session {session_id}: {e}")
            return False
    
    @staticmethod
    async def get_draft_session_safe(session_id: str) -> Optional[DraftSession]:
        """Safely get a draft session with error handling."""
        try:
            return await get_draft_session(session_id)
        except Exception as e:
            logger.error(f"Error fetching draft session {session_id}: {e}")
            return None


class EmbedHelper:
    """Helper class for creating and managing Discord embeds."""
    
    @staticmethod
    def split_content_for_embed(content, include_header=False, max_length=1000):
        """
        Split content into chunks that fit within Discord's embed field value limits.
        
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
                chunks.append(current_chunk)
                current_chunk = header if header else ""
                
                if current_chunk:
                    current_chunk += '\n' + line
                else:
                    current_chunk = line
            else:
                current_chunk += '\n' + line
        
        if current_chunk:
            chunks.append(current_chunk)
            
        return chunks
    
    @staticmethod
    def add_links_to_embed_safely(embed, links, base_name, team_color=""):
        """
        Add links to an embed, splitting them into multiple fields if needed.
        
        Args:
            embed: The discord.Embed object to add fields to
            links: List of link strings to add
            base_name: Base name for the embed field
            team_color: Optional color indicator ('red', 'blue', or '') for emoji prefixing
        """
        if not links:
            return
        
        content = "\n".join(links)
        
        if len(content) <= 1000:
            emoji = "🔴 " if team_color == "red" else "🔵 " if team_color == "blue" else ""
            embed.add_field(
                name=f"{emoji}{base_name}",
                value=content,
                inline=False
            )
            return
        
        chunks = EmbedHelper.split_content_for_embed(links)
        emoji = "🔴 " if team_color == "red" else "🔵 " if team_color == "blue" else ""
        
        for i, chunk in enumerate(chunks):
            suffix = "" if i == 0 else f" (part {i+1})"
            value = chunk if isinstance(chunk, str) else "\n".join(chunk)
            embed.add_field(
                name=f"{emoji}{base_name}{suffix}",
                value=value,
                inline=False
            )


class PermissionHelper:
    """Helper class for permission and authorization checks."""
    
    @staticmethod
    async def check_timeout_role(interaction: discord.Interaction) -> bool:
        """
        Check if user has the timeout role.
        
        Returns:
            bool: True if user has timeout role, False otherwise
        """
        config = get_config(interaction.guild_id)
        timeout_role_name = config.get("roles", {}).get("timeout")
        
        if timeout_role_name and discord.utils.get(interaction.user.roles, name=timeout_role_name):
            return True
        return False
    
    @staticmethod
    async def check_user_in_draft(interaction: discord.Interaction, session_id: str) -> tuple[bool, Optional[DraftSession]]:
        """
        Check if user is in the draft session.
        
        Returns:
            tuple: (is_in_draft, draft_session)
        """
        draft_session = await DatabaseHelper.get_draft_session_safe(session_id)
        if not draft_session:
            return False, None
            
        user_id = str(interaction.user.id)
        is_in_draft = user_id in (draft_session.sign_ups or {})
        return is_in_draft, draft_session


class ResponseHelper:
    """Helper class for standardized responses."""
    
    @staticmethod
    async def send_error(interaction: discord.Interaction, message: str, ephemeral: bool = True):
        """Send an error message, handling both deferred and non-deferred interactions."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(message, ephemeral=ephemeral)
        except Exception as e:
            logger.error(f"Error sending error message: {e}")
    
    @staticmethod
    async def send_success(interaction: discord.Interaction, message: str, ephemeral: bool = True):
        """Send a success message, handling both deferred and non-deferred interactions."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(message, ephemeral=ephemeral)
        except Exception as e:
            logger.error(f"Error sending success message: {e}")


class BaseView(View):
    """Base class for all custom views with common functionality."""
    
    def __init__(self, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self._error_handler = ResponseHelper.send_error
        self._success_handler = ResponseHelper.send_success
    
    async def on_error(self, error: Exception, item: discord.ui.Item, interaction: discord.Interaction):
        """Global error handler for view interactions."""
        logger.error(f"Error in {self.__class__.__name__}: {error}", exc_info=True)
        await self._error_handler(interaction, "An error occurred. Please try again.")
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Override this method to add custom interaction checks."""
        return True


class BaseModal(discord.ui.Modal):
    """Base class for all custom modals with common functionality."""
    
    def __init__(self, title: str, timeout: Optional[float] = 300):
        super().__init__(title=title, timeout=timeout)
        self._error_handler = ResponseHelper.send_error
        self._success_handler = ResponseHelper.send_success
    
    async def on_error(self, error: Exception, interaction: discord.Interaction):
        """Global error handler for modal interactions."""
        logger.error(f"Error in {self.__class__.__name__}: {error}", exc_info=True)
        await self._error_handler(interaction, "An error occurred. Please try again.")


class CallbackButton(Button):
    """Enhanced button class with custom callback support."""
    
    def __init__(self, *, label, style, custom_id, custom_callback: Callable, disabled=False, **kwargs):
        super().__init__(label=label, style=style, custom_id=custom_id, disabled=disabled, **kwargs)
        self.custom_callback = custom_callback
    
    async def callback(self, interaction: discord.Interaction):
        """Execute the custom callback."""
        try:
            await self.custom_callback(interaction, self)
        except Exception as e:
            logger.error(f"Error in button callback {self.custom_id}: {e}", exc_info=True)
            await ResponseHelper.send_error(interaction, "An error occurred processing your request.")


class ButtonStateManager:
    """Helper class for managing button states in views."""
    
    @staticmethod
    def disable_all_except(view: View, keep_enabled: List[str]):
        """
        Disable all buttons in a view except those with custom_ids in keep_enabled list.
        
        Args:
            view: The discord.ui.View containing buttons
            keep_enabled: List of custom_id patterns to keep enabled
        """
        for item in view.children:
            if isinstance(item, Button):
                should_enable = any(pattern in item.custom_id for pattern in keep_enabled)
                item.disabled = not should_enable
    
    @staticmethod
    def update_button_by_id(view: View, custom_id_pattern: str, **kwargs):
        """
        Update a button's properties by its custom_id pattern.
        
        Args:
            view: The discord.ui.View containing buttons
            custom_id_pattern: Pattern to match in custom_id
            **kwargs: Properties to update (label, style, disabled, etc.)
        """
        for item in view.children:
            if isinstance(item, Button) and custom_id_pattern in item.custom_id:
                for key, value in kwargs.items():
                    if hasattr(item, key):
                        setattr(item, key, value)
                return True
        return False


# Global processing locks to prevent race conditions
PROCESSING_LOCKS = {
    'rooms_pairings': {},
    'teams_creation': {},
    'ready_check_cooldowns': {}
}


class ProcessingLockManager:
    """Manager for handling processing locks to prevent race conditions."""
    
    @staticmethod
    def is_locked(lock_type: str, session_id: str) -> bool:
        """Check if a process is locked for a given session."""
        return PROCESSING_LOCKS.get(lock_type, {}).get(session_id, False)
    
    @staticmethod
    def acquire_lock(lock_type: str, session_id: str) -> bool:
        """
        Try to acquire a lock for a process.
        
        Returns:
            bool: True if lock acquired, False if already locked
        """
        if lock_type not in PROCESSING_LOCKS:
            PROCESSING_LOCKS[lock_type] = {}
        
        if PROCESSING_LOCKS[lock_type].get(session_id, False):
            return False
        
        PROCESSING_LOCKS[lock_type][session_id] = True
        return True
    
    @staticmethod
    def release_lock(lock_type: str, session_id: str):
        """Release a lock for a process."""
        if lock_type in PROCESSING_LOCKS and session_id in PROCESSING_LOCKS[lock_type]:
            del PROCESSING_LOCKS[lock_type][session_id]
    
    @staticmethod
    async def with_lock(lock_type: str, session_id: str, coroutine, 
                       error_interaction: Optional[discord.Interaction] = None):
        """
        Execute a coroutine with a processing lock.
        
        Args:
            lock_type: Type of lock to acquire
            session_id: Session ID for the lock
            coroutine: Async function to execute
            error_interaction: Optional interaction for error messages
            
        Returns:
            Result of the coroutine or None if lock couldn't be acquired
        """
        if not ProcessingLockManager.acquire_lock(lock_type, session_id):
            if error_interaction:
                await ResponseHelper.send_error(
                    error_interaction, 
                    f"This operation is already in progress. Please wait."
                )
            return None
        
        try:
            return await coroutine
        finally:
            ProcessingLockManager.release_lock(lock_type, session_id)


# Cooldown manager for ready checks
class CooldownManager:
    """Manager for handling cooldowns on operations."""
    
    @staticmethod
    async def check_cooldown(cooldown_dict: Dict[str, datetime], key: str, 
                           cooldown_seconds: int, interaction: discord.Interaction) -> bool:
        """
        Check if an operation is on cooldown.
        
        Returns:
            bool: True if operation can proceed, False if on cooldown
        """
        current_time = datetime.now()
        cooldown_end_time = cooldown_dict.get(key)
        
        if cooldown_end_time and current_time < cooldown_end_time:
            remaining_seconds = int((cooldown_end_time - current_time).total_seconds())
            await ResponseHelper.send_error(
                interaction,
                f"This operation is on cooldown. Please wait {remaining_seconds} seconds."
            )
            return False
        
        # Set new cooldown
        cooldown_dict[key] = current_time + timedelta(seconds=cooldown_seconds)
        
        # Schedule cooldown removal
        asyncio.create_task(CooldownManager._remove_cooldown_after(
            cooldown_dict, key, cooldown_seconds
        ))
        
        return True
    
    @staticmethod
    async def _remove_cooldown_after(cooldown_dict: Dict[str, datetime], 
                                   key: str, seconds: int):
        """Remove a cooldown after the specified time."""
        await asyncio.sleep(seconds)
        if key in cooldown_dict:
            del cooldown_dict[key]