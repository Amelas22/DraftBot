import asyncio
import socketio
from loguru import logger
from functools import wraps
import random
import aiohttp
import json
import os
import pytz
import urllib.parse
import discord
from datetime import datetime, timedelta
from dotenv import load_dotenv
from config import get_draftmancer_websocket_url, get_draftmancer_base_url, get_draftmancer_session_url
from database.db_session import db_session
from models.draft_session import DraftSession
from models.match import MatchResult
from bot_registry import get_bot
from session import AsyncSessionLocal
from sqlalchemy import select
from helpers.digital_ocean_helper import DigitalOceanHelper
from helpers.magicprotools_helper import MagicProtoolsHelper
from notification_service import send_ready_check_dms
from services.draft_socket_client import DraftSocketClient

# Constants
READY_CHECK_INSTRUCTIONS = (
    "If the seating order is wrong, or if someone missed the ready check, please run `/ready` again — this will reset the seating order and start a new ready check. "
    "You can also use `/mutiny` to take control if needed."
)

# Victory detection constants
VICTORY_CHECK_TIMEOUT = 600  # Maximum 10 minutes to wait for victory
VICTORY_CHECK_INTERVAL = 30  # Check every 30 seconds
VICTORY_CHECK_INITIAL_DELAY = 10  # Initial delay for immediate victory detection

# Load environment variables
load_dotenv()

# Global registry to track active manager instances
ACTIVE_MANAGERS = {}

def exponential_backoff(max_retries=10, base_delay=1):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    result = await func(*args, **kwargs)
                    if result:  # If the function succeeds
                        return result
                except Exception as e:
                    logger.error(f"Attempt {retries + 1} failed: {e}")
                
                retries += 1
                if retries < max_retries:
                    delay = (base_delay * 2 ** retries) + (random.uniform(0, 1))  # Add jitter
                    logger.info(f"Backing off for {delay:.2f} seconds before retry {retries + 1}")
                    await asyncio.sleep(delay)
            
            return False  # All retries failed
        return wrapper
    return decorator

class DraftSetupManager:
    async def _on_connect(self):
        """Handler for 'connect' event."""
        self.logger.info(f"Successfully connected to the websocket for draft_id: DB{self.draft_id}")
        self._is_connecting = False

    async def _on_connect_error(self, data):
        """Handler for 'connect_error' event."""
        self.logger.warning(f"Connection to the websocket failed for draft_id: DB{self.draft_id}. Data: {data}")
        self._is_connecting = False

    async def _on_disconnect(self):
        """Handler for 'disconnect' event."""
        self.logger.info(f"Disconnected from the websocket for draft_id: DB{self.draft_id}")
        self._is_connecting = False

    def __init__(self, session_id: str, draft_id: str, cube_id: str):
        self.session_id = session_id
        self.draft_id = draft_id
        self.cube_id = cube_id
        
        # Configure contextual logger for this instance
        self.logger = logger.bind(draft_id=draft_id, session_id=session_id)
        
        # Initialize DraftSocketClient
        self.socket_client = DraftSocketClient(resource_id=f"DB{draft_id}")
        
        # Register standard events
        self.socket_client.sio.on('connect', self._on_connect)
        self.socket_client.sio.on('connect_error', self._on_connect_error)
        self.socket_client.sio.on('disconnect', self._on_disconnect)
        
        # Register Draftmancer specific events
        self.socket_client.sio.on('setReady', self._on_set_ready)
        self.socket_client.sio.on('endDraft', self._on_end_draft)
        self.socket_client.sio.on('draftPaused', self._on_draft_paused)
        self.socket_client.sio.on('draftResumed', self._on_draft_resumed)
        self.socket_client.sio.on('sessionUsers', self._on_session_users)
        self.socket_client.sio.on('storedSessionSettings', self._on_stored_session_settings)
        self.socket_client.sio.on('draftLog', self._on_draft_log)
        
        # NOTE: self.sio is now deprecated, access via self.socket_client.sio if absolutely needed
        # Mapping properties for compatibility
        self.sio = self.socket_client.sio

        # Cube import state
        self.cube_imported = False

        # Seating Order Variables
        self.session_users = []
        self.seating_attempts = 0
        self.seating_order_set = False
        self.last_db_check_time = None
        self.db_check_cooldown = 15
        self.expected_user_count = 0
        self.users_count = 0  # Current count of non-bot users in the session
        self.desired_seating_order = None

        # Connection state tracking
        # Note: Connection locking is handled by DraftSocketClient._connection_lock
        self._is_connecting = False  # Tracks if connection attempt is in progress (set by event handlers)
        self._should_disconnect = False
        self._seating_lock = asyncio.Lock()  # Lock for seating attempts

        # Ready Check variables 
        self.ready_check_active = False
        self.ready_check_message_id = None
        self.ready_users = set()
        self.ready_check_timer = None
        self.post_timeout_ready_users = set()
        self.draft_channel_id = None  # Will be populated from database
        self.drafting = False
        self.draftPaused = False
        self.draft_cancelled = False
        self.removing_unexpected_user = False
        self.timeout_message_id = None

        # Draft logs variables
        self.logs_collection_attempted = False
        self.logs_collection_in_progress = False
        self.logs_collection_success = False
        self.session_type = "team"  # Default to team drafts
        self.guild_id = None
        
        # Initialize helpers
        self.mpt_helper = MagicProtoolsHelper()
        self.discord_client = None

        # Status tracking variables
        self.status_message_id = None
        self.last_status_update = None
        self.session_status = {
            'present_users': [],
            'missing_users': [],
            'unexpected_users': [],
            'updated_at': datetime.now().strftime('%H:%M:%S')
        }

        # Add storage for the current draft log
        self.current_draft_log = None
            
        # Register this instance in the global registry
        ACTIVE_MANAGERS[session_id] = self
        self.logger.info(f"Registered manager for session {session_id} in active managers registry")
        
        # Initialize state variables for new DraftSocketClient integration
        self.ready_check_timer_task = None
        self.ready_check_message = None
        self.ready_check_view = None
        self.num_expected_users = 0
        self.session_users_received = False
        self.ready_check_in_progress = False
        self.settings_updated = False
        self.log_collection_task = None
        self.log_collection_retry_count = 0 
        self.MAX_LOG_COLLECTION_RETRIES = 5
        self.log_release_vote_active = False
        
    # Add a listener to capture draft logs
    async def _on_draft_log(self, draft_log):
        self.logger.info(f"Received draft log for session: {draft_log.get('sessionID')}")
        # Store the draft log
        self.current_draft_log = draft_log
        
    # Listen for user changes in ready state status
    async def _on_set_ready(self, userID, readyState):
        await self.handle_user_ready_update(userID, readyState)
    
    # Listen for Draft Completion
    async def _on_end_draft(self, data=None):
        logger.info(f"Draft ended event received: {data}")
        self.drafting = False
        self.draftPaused = False
        
        if self.draft_cancelled:
            logger.info("Draft was manually cancelled - no additional announcement needed")
            self.draft_cancelled = False  # Reset the flag for future drafts
        else:
            logger.info("Draft completed naturally - creating rooms and scheduling log collection")
            bot = get_bot()
            guild = bot.get_guild(int(self.guild_id))
            channel = bot.get_channel(int(self.draft_channel_id))
            from views import PersistentView
            if guild:
                # Attempt to create rooms and pairings
                result = await PersistentView.create_rooms_pairings(bot, guild, self.session_id, session_type=self.session_type)
                if channel:
                    # Only announce if rooms were actually created
                    if result:
                        await channel.send("Rooms and Pairings have been created!")
                    else:
                        # Check if rooms already existed

                        async with AsyncSessionLocal() as db_session:
                            stmt = select(DraftSession).filter(DraftSession.session_id == self.session_id)
                            session = await db_session.scalar(stmt)
                            if session and session.draft_chat_channel:
                                self.logger.info(f"Rooms already existed for session {self.session_id} - skipping creation")
                            else:
                                await channel.send("Failed to create rooms and pairings. Check logs for details.")
            else:
                self.logger.info("Could not find guild")

    # Listen for Pause or Unpause (Resume)
    async def _on_draft_paused(self, data):
        self.logger.info(f"Draft paused event received: {data}")
        self.draftPaused = True

    async def _on_draft_resumed(self, data):
        self.logger.info(f"Draft resumed event received: {data}")
        self.draftPaused = False
        
    # Listen for user changes in the session
    async def _on_session_users(self, users):
        self.logger.debug(f"Raw users data received: {users}")
        
        # Store the complete user data
        previous_users = self.session_users.copy() if hasattr(self, 'session_users') else []
        
        # Get previous non-bot users for comparison
        previous_non_bot_users = [user for user in previous_users if user.get('userName') != 'DraftBot']
        previous_count = self.users_count
        
        # Update to new users list
        self.session_users = users
        
        # Count current non-bot users
        non_bot_users = [user for user in users if user.get('userName') != 'DraftBot']
        self.users_count = len(non_bot_users)  # Only count non-bot users
        
        self.logger.info(
            f"Users update: Total users={len(users)}, Non-bot users={self.users_count}, "
            f"User IDs={[user.get('userID') for user in non_bot_users]}"
        )
        
        # Check if user count decreased (someone left)
        if self.ready_check_active and previous_count > self.users_count:
            # Someone left during an active ready check - identify who left
            previous_usernames = {user.get('userName') for user in previous_non_bot_users}
            current_usernames = {user.get('userName') for user in non_bot_users}
            left_users = previous_usernames - current_usernames
            
            left_user_name = next(iter(left_users)) if left_users else "Unknown"
            self.logger.info(f"User {left_user_name} left during active ready check - invalidating ready check")
            await self.invalidate_ready_check(left_user_name)
        
        if self.ready_check_active and previous_count < self.users_count:
            # Someone joined during an active ready check - identify who joined
            previous_usernames = {user.get('userName') for user in previous_non_bot_users}
            current_usernames = {user.get('userName') for user in non_bot_users}
            left_users = current_usernames - previous_usernames
            
            left_user_name = next(iter(left_users)) if left_users else "Unknown"
            self.logger.info(f"User {left_user_name} joined during active ready check - invalidating ready check")
            await self.invalidate_ready_check(left_user_name)
        
        # Check if user list changed and update status message if needed
        if previous_count != self.users_count or len(previous_users) != len(users):
            self.logger.info(f"User count changed from {previous_count} to {self.users_count}, updating status message")
            await self.update_status_message_after_user_change()
        
        # IMPORTANT: If we've reached the expected count of users or seating order 
        # is not set and we have all users, check immediately
        if ((self.expected_user_count is not None and 
            previous_count < self.expected_user_count and 
            self.users_count >= self.expected_user_count) or
            (not self.seating_order_set and 
             self.users_count >= self.expected_user_count and
             self.expected_user_count > 0)):
            
            self.logger.info(f"Reached expected user count or need to reset seating! Attempting seating order. on_session_users")
            await self.check_session_stage_and_organize()
            return
        
        # Otherwise, check on our regular schedule
        current_time = datetime.now()
        if (self.last_db_check_time is None or 
            (current_time - self.last_db_check_time).total_seconds() > self.db_check_cooldown):
            
            self.last_db_check_time = current_time
            self.logger.info("check session stage from on_session_users")
            await self.check_session_stage_and_organize()

    async def _on_stored_session_settings(self, data):
        self.logger.info(f"Received updated session settings: {data}")

    # ============== Helper Methods ==============

    async def _get_draft_channel(self):
        """
        Get the draft channel with fallback to fetch if not cached.
        Returns the channel or None if not found.
        """
        if not self.draft_channel_id:
            self.logger.warning("No draft_channel_id set")
            return None

        bot = get_bot()
        if not bot:
            self.logger.error("Bot instance not available")
            return None

        channel = bot.get_channel(int(self.draft_channel_id))
        if not channel:
            try:
                channel = await bot.fetch_channel(int(self.draft_channel_id))
            except Exception as e:
                self.logger.error(f"Error fetching channel {self.draft_channel_id}: {e}")

        if not channel:
            self.logger.error(f"Could not find channel with ID {self.draft_channel_id}")

        return channel

    async def _get_draft_session_from_db(self):
        """
        Fetch the draft session from database using session_id.
        Returns DraftSession or None if not found.
        """
        try:
            async with db_session() as session:
                stmt = select(DraftSession).filter(DraftSession.session_id == self.session_id)
                result = await session.execute(stmt)
                return result.scalar_one_or_none()
        except Exception as e:
            self.logger.error(f"Error fetching draft session from database: {e}")
            return None

    def _compute_user_status(self, sign_ups: dict = None):
        """
        Compute present, missing, and unexpected users based on session users and sign-ups.

        Args:
            sign_ups: Dict of user_id -> username. If None, returns empty sets.

        Returns:
            Dict with 'present', 'missing', 'unexpected' keys containing username sets.
        """
        if not sign_ups:
            return {'present': set(), 'missing': set(), 'unexpected': set()}

        session_usernames = {
            user.get('userName') for user in self.session_users
            if user.get('userName') != 'DraftBot'
        }
        signup_usernames = set(sign_ups.values())

        return {
            'present': session_usernames.intersection(signup_usernames),
            'missing': signup_usernames - session_usernames,
            'unexpected': session_usernames - signup_usernames
        }

    async def _update_or_send_message(self, channel, message_id: str, content=None, embed=None, view=None):
        """
        Try to update an existing message, fallback to sending a new one.

        Args:
            channel: Discord channel to send/edit in
            message_id: ID of message to try editing (can be None)
            content: Text content for the message
            embed: Discord embed for the message
            view: Discord view for the message

        Returns:
            The message object if successful, None otherwise.
        """
        # Try to edit existing message
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
                await message.edit(content=content, embed=embed, view=view)
                return message
            except discord.NotFound:
                self.logger.debug(f"Message {message_id} not found, will send new")
            except Exception as e:
                self.logger.warning(f"Could not update message {message_id}: {e}")

        # Fallback to sending new message
        try:
            return await channel.send(content=content, embed=embed, view=view)
        except Exception as e:
            self.logger.error(f"Failed to send message: {e}")
            return None

    async def _safe_delete_message(self, channel, message_id: str):
        """
        Safely delete a message with proper error handling.

        Args:
            channel: Discord channel containing the message
            message_id: ID of message to delete

        Returns:
            True if deleted successfully, False otherwise.
        """
        if not message_id or not channel:
            return False

        try:
            message = await channel.fetch_message(int(message_id))
            await message.delete()
            return True
        except discord.NotFound:
            self.logger.debug(f"Message {message_id} already deleted")
            return True  # Consider it a success if already gone
        except discord.Forbidden:
            self.logger.warning(f"No permission to delete message {message_id}")
        except Exception as e:
            self.logger.error(f"Error deleting message {message_id}: {e}")
        return False

    async def _update_draft_session_field(self, field_name: str, value):
        """
        Update a single field in the draft session database record.

        Args:
            field_name: Name of the field to update
            value: New value for the field

        Returns:
            True if successful, False otherwise.
        """
        try:
            async with db_session() as session:
                stmt = select(DraftSession).filter(DraftSession.session_id == self.session_id)
                result = await session.execute(stmt)
                draft_session = result.scalar_one_or_none()
                if draft_session:
                    setattr(draft_session, field_name, value)
                    await session.commit()
                    return True
                else:
                    self.logger.error(f"Draft session {self.session_id} not found for update")
                    return False
        except Exception as e:
            self.logger.error(f"Error updating draft session field {field_name}: {e}")
            return False

    # ============== End Helper Methods ==============

    async def regenerate_draft_session(self):
        """
        Generate a new draft_id and update the database.
        Used when the bot cannot access the original Draftmancer session.
        Returns True if successful, False otherwise.
        """
        try:
            # Generate new draft_id (same format as SessionDetails)
            new_draft_id = ''.join(random.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
            new_draft_link = get_draftmancer_session_url(new_draft_id)

            self.logger.info(f"Regenerating draft session: old={self.draft_id}, new={new_draft_id}")

            # Update the database
            async with db_session() as session:
                stmt = select(DraftSession).filter(DraftSession.session_id == self.session_id)
                result = await session.execute(stmt)
                draft_session = result.scalar_one_or_none()

                if not draft_session:
                    self.logger.error(f"Could not find draft session {self.session_id} to update")
                    return False

                # Update the draft_id and draft_link
                draft_session.draft_id = new_draft_id
                draft_session.draft_link = new_draft_link
                await session.commit()

                self.logger.info(f"Updated draft session in database with new draft_id: {new_draft_id}")

            # Update local state
            old_draft_id = self.draft_id
            self.draft_id = new_draft_id
            self.cube_imported = False  # Reset so we try to import again

            # Update the logger context
            self.logger = logger.bind(
                draft_id=self.draft_id,
                session_id=self.session_id,
                cube_id=self.cube_id
            )

            # Disconnect from old session if connected
            if self.socket_client.connected:
                await self.socket_client.disconnect()
                self.logger.info(f"Disconnected from old session DB{old_draft_id}")

            return True

        except Exception as e:
            self.logger.error(f"Error regenerating draft session: {e}")
            return False

    async def _notify_bot_no_longer_managing(self):
        """Send a notification that the bot can no longer manage this draft session."""
        channel = await self._get_draft_channel()
        if not channel:
            return

        embed = discord.Embed(
            title="Bot No Longer Managing This Draft",
            description=(
                "The bot is no longer the owner of this Draftmancer session and cannot "
                "manage it automatically. This can happen if someone else took control "
                "of the session or if there was a connection issue."
            ),
            color=discord.Color.orange()
        )
        embed.add_field(
            name="What To Do",
            value=(
                "**The draft can continue manually:**\n"
                "• The session owner in Draftmancer can import the cube and start the draft\n"
                "• Use `/mutiny` if you need to create a new bot-managed session\n"
                "• Match results can still be reported normally after the draft"
            ),
            inline=False
        )

        if await self._update_or_send_message(channel, None, embed=embed):
            self.logger.info(f"Sent 'bot no longer managing' notification to channel {self.draft_channel_id}")

    async def connect_to_new_session(self):
        """Connect to the new Draftmancer session after regeneration."""
        try:
            websocket_url = get_draftmancer_websocket_url(self.draft_id)
            self.logger.info(f"Connecting to new session at {websocket_url}")

            connection_successful = await self.socket_client.connect_with_retry(websocket_url)
            if not connection_successful:
                self.logger.error("Failed to connect to new session")
                return False

            return True
        except Exception as e:
            self.logger.error(f"Error connecting to new session: {e}")
            return False

    async def collect_draft_logs(self):
        """Collect draft logs and process them"""
        if self.logs_collection_attempted or self.logs_collection_in_progress:
            self.logger.info("Log collection already attempted or in progress, skipping")
            return
        
        self.logs_collection_in_progress = True
        self.logger.info("Starting draft log collection")
        
        try:
            # Get session type information from database
            await self.fetch_draft_info()
            
            # Attempt to fetch draft log data
            for attempt in range(36):  # Try up to 36 times (3 hours)
                data_fetched = await self.fetch_draft_log_data()
                if data_fetched:
                    self.logs_collection_success = True
                    self.logger.info(f"Successfully collected draft logs on attempt {attempt+1}")
                    break
                
                self.logger.info(f"Draft log data not available on attempt {attempt+1}, waiting 5 minutes before retrying")
                await asyncio.sleep(300)  # Wait 5 minutes before retrying
            
            if not self.logs_collection_success:
                self.logger.warning("Failed to collect draft logs after multiple attempts")
        
        except Exception as e:
            self.logger.exception(f"Error collecting draft logs: {e}")
        
        finally:
            self.logs_collection_attempted = True
            self.logs_collection_in_progress = False
            
            # If we collected logs successfully, check victory status before disconnect
            if self.logs_collection_success:
                self.logger.info("Log collection successful, checking victory status before disconnect")
                await self._handle_victory_aware_disconnect()
            else:
                self.logger.info("Log collection failed, bot will remain connected")

    async def fetch_draft_log_data(self):
        """Fetch draft log data from the Draftmancer API"""
        base_url = get_draftmancer_base_url()
        url = f"{base_url}/getDraftLog/DB{self.draft_id}"
        
        self.logger.info(f"Fetching draft log data from {url}")
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        draft_data = await response.json()
                        
                        # Check if we have user picks
                        has_picks = False
                        for user_data in draft_data.get("users", {}).values():
                            if user_data.get("picks") and len(user_data.get("picks")) > 0:
                                has_picks = True
                                break
                        
                        if not has_picks:
                            self.logger.info(f"Draft log data for {self.draft_id} has no picks yet")
                            return False
                        
                        # Save the data
                        await self.save_draft_log_data(draft_data)
                        return True
                    else:
                        self.logger.warning(f"Failed to fetch draft log data: status code {response.status}")
                        return False
            except Exception as e:
                self.logger.error(f"Exception while fetching draft log data: {e}")
                return False

    async def save_draft_log_data(self, draft_data):
        """Save draft log data to database and process it"""
        try:

            # Save to DigitalOcean Spaces
            upload_successful = await self.save_to_digitalocean_spaces(draft_data)
            
            # Extract and store first picks for each user and pack
            draftmancer_user_picks = {}
            for user_id, user_data in draft_data["users"].items():
                user_pack_picks = self.get_pack_first_picks(draft_data, user_id)
                draftmancer_user_picks[user_id] = user_pack_picks
            
            # We need to convert Draftmancer user IDs to Discord user IDs
            discord_user_pack_picks = {}
            
            # Use a fresh database session for this operation
            async with db_session() as session:
                # Get the draft session within this session context
                stmt = select(DraftSession).filter(DraftSession.session_id == self.session_id)
                result = await session.execute(stmt)
                draft_session = result.scalar_one_or_none()
                
                if not draft_session:
                    self.logger.warning(f"No draft session found for session_id: {self.session_id}")
                    return False

                # Get Discord IDs from sign_ups
                if draft_session.sign_ups:
                    # Get list of Discord user IDs from sign_ups
                    discord_ids = list(draft_session.sign_ups.keys())
                    
                    # Sort users by seat number
                    sorted_users = sorted(
                        [(user_id, user_data) for user_id, user_data in draft_data["users"].items()],
                        key=lambda item: item[1].get("seatNum", 999)
                    )
                    
                    # Map Draftmancer user IDs to Discord user IDs based on draft seat order
                    for idx, (draft_user_id, _) in enumerate(sorted_users):
                        if idx < len(discord_ids):
                            discord_id = discord_ids[idx]
                            if draft_user_id in draftmancer_user_picks:
                                discord_user_pack_picks[discord_id] = draftmancer_user_picks[draft_user_id]
                
                # Update draft session directly in this session context
                if upload_successful:
                    draft_session.data_received = True
                    draft_session.pack_first_picks = discord_user_pack_picks
                    self.logger.info(f"Stored first picks for {len(discord_user_pack_picks)} users with Discord IDs as keys")
                else:
                    draft_session.draft_data = draft_data
                    self.logger.info(f"Draft log data saved in database for {self.draft_id}")
                
                # Commit changes
                await session.commit()
            
            # Send MagicProTools embed to Discord if bot is available
            if self.guild_id:
                await self.send_magicprotools_embed(draft_data)
            
            return upload_successful
                
        except Exception as e:
            self.logger.exception(f"Error saving draft log data: {e}")
            return False

    async def save_to_digitalocean_spaces(self, draft_data):
        """Upload draft log data to DigitalOcean Spaces"""
        start_time = draft_data.get("time")
        draft_id = draft_data.get("sessionID")
        
        # Create DigitalOcean helper
        do_helper = DigitalOceanHelper()
        
        try:
            # Determine folder based on session type
            folder = "swiss" if self.session_type == "swiss" else "team"
            filename = f'{self.cube_id}-{start_time}-{draft_id}.json'
            
            # Upload the JSON data
            result = await do_helper.upload_json(draft_data, folder, filename)

            if result.success:
                self.logger.info(f"Draft log data uploaded to DigitalOcean Space: {result.object_path}")
                
                # If upload successful, also generate and upload MagicProTools format logs
                await self.process_draft_logs_for_magicprotools(draft_data, do_helper)
                
                return True
            else:
                self.logger.warning("Failed to upload draft log data to DigitalOcean Spaces")
                return False
                
        except Exception as e:
            self.logger.error(f"Error uploading to DigitalOcean Space: {e}")
            return False

    async def process_draft_logs_for_magicprotools(self, draft_data, do_helper):
        """Process the draft log and generate formatted logs for each player."""
        try:
            session_id = draft_data.get("sessionID")
            
            # Use the MagicProtoolsHelper to upload draft logs
            user_mpt_data = await self.mpt_helper.upload_draft_logs(
                draft_data,
                session_id,
                self.session_type
            )
            
            if user_mpt_data:
                self.logger.info(f"All MagicProTools format logs generated and uploaded for draft {session_id}")
                return True
            else:
                self.logger.warning(f"No MagicProTools format logs were generated for draft {session_id}")
                return False
        except Exception as e:
            self.logger.error(f"Error generating MagicProTools format logs: {e}")
            return False

    # This method has been replaced by using the MagicProtoolsHelper.convert_to_magicprotools_format method

    # This method has been replaced by using the MagicProtoolsHelper.submit_to_api method

    async def send_magicprotools_embed(self, draft_data):
        """Find draft-logs channel and send the embed if found."""
        try:
            # Find the guild
            bot = get_bot()
            guild = bot.get_guild(int(self.guild_id))
            if not guild:
                self.logger.warning(f"Could not find guild with ID {self.guild_id}")
                return
            
            # Find a channel named "draft-logs"
            draft_logs_channel = None
            for channel in guild.channels:
                if channel.name.lower() == "draft-logs" and hasattr(channel, "send"):
                    draft_logs_channel = channel
                    break
            
            if draft_logs_channel:
                # Generate the embed and send it
                embed = await self.generate_magicprotools_embed(draft_data)
                message = await draft_logs_channel.send(embed=embed)
                self.logger.info(f"Sent MagicProTools links to #{draft_logs_channel.name} in {guild.name}")
                
                # Save the channel and message IDs to the database
                async with db_session() as session:
                    # Get a fresh reference to the draft session
                    stmt = select(DraftSession).filter(DraftSession.session_id == self.session_id)
                    result = await session.execute(stmt)
                    draft_session = result.scalar_one_or_none()
                    
                    if draft_session:
                        draft_session.logs_channel_id = str(draft_logs_channel.id)
                        draft_session.logs_message_id = str(message.id)
                        await session.commit()
                        self.logger.info(f"Saved logs channel and message IDs for session {self.session_id}")
                    
                        # Update victory messages to include the logs link
                        # Import the function here to avoid circular imports
                        from utils import check_and_post_victory_or_draw
                        try:
                            await check_and_post_victory_or_draw(bot, self.session_id)
                            self.logger.info(f"Successfully updated victory messages with logs link for session {self.session_id}")
                        except Exception as e:
                            self.logger.error(f"Error updating victory messages with logs link: {e}")
            else:
                self.logger.warning(f"No 'draft-logs' channel found in guild {guild.name}, skipping embed message")
        except Exception as e:
            self.logger.error(f"Error sending MagicProTools embed: {e}")
            
    async def generate_magicprotools_embed(self, draft_data):
        """Generate a Discord embed with MagicProTools links for all drafters"""
        try:
            # Create DigitalOcean helper for getting URLs
            do_helper = DigitalOceanHelper()
            session_id = draft_data.get("sessionID")
            folder = "swiss" if self.session_type == "swiss" else "team"
            
            # Get the draft session to access sign_ups and start time
            async with db_session() as session:
                # Get draft session in this session context
                stmt = select(DraftSession).filter(DraftSession.session_id == self.session_id)
                result = await session.execute(stmt)
                draft_session = result.scalar_one_or_none()
                
                if not draft_session:
                    self.logger.warning(f"Draft session not found for session ID: {self.session_id}")
                    sign_ups = {}
                    formatted_start_time = "Unknown"
                    player_records = {}
                else:
                    sign_ups = draft_session.sign_ups or {}
                    if draft_session.teams_start_time:
                        start_time = draft_session.teams_start_time
                        # Format the start time for Discord
                        start_timestamp = int(start_time.timestamp())
                        formatted_start_time = f"<t:{start_timestamp}:F>"
                    else:
                        formatted_start_time = "Unknown"
                    
                    # Get all match results for this session and calculate player records
                    player_records = {}
                    if draft_session.victory_message_id_draft_chat:  # Only fetch if victory message exists
                        match_results_stmt = select(MatchResult).filter(MatchResult.session_id == self.session_id)
                        match_results_result = await session.execute(match_results_stmt)
                        match_results = match_results_result.scalars().all()
                        
                        # Calculate win-loss records for each player
                        for match in match_results:
                            # Skip matches without a winner
                            if not match.winner_id:
                                continue
                                
                            # Add win for winner, loss for loser
                            if match.winner_id == match.player1_id:
                                # Player 1 won
                                player_records.setdefault(match.player1_id, {"wins": 0, "losses": 0})["wins"] += 1
                                player_records.setdefault(match.player2_id, {"wins": 0, "losses": 0})["losses"] += 1
                            elif match.winner_id == match.player2_id:
                                # Player 2 won
                                player_records.setdefault(match.player2_id, {"wins": 0, "losses": 0})["wins"] += 1
                                player_records.setdefault(match.player1_id, {"wins": 0, "losses": 0})["losses"] += 1
            
            embed = discord.Embed(
                title=f"Draft Log: Cube: {self.cube_id}, Session:{session_id}",
                description=f"View your draft in MagicProTools with the links below:\n\n**Draft Start:** {formatted_start_time}",
                color=0x3498db  # Blue color
            )
            
            # Get list of sign_ups keys (Discord user IDs) and values (display names or dictionaries)
            sign_up_discord_ids = list(sign_ups.keys())
            sign_up_display_names = list(sign_ups.values())
            
            # Create mapping of user index to Discord display name and ID
            # First sort users by seat number
            sorted_users = sorted(
                [(user_id, user_data) for user_id, user_data in draft_data["users"].items()],
                key=lambda item: item[1].get("seatNum", 999)
            )
            
            # Now map Discord display names and IDs to sorted users
            discord_name_by_user_id = {}
            discord_id_by_user_id = {}
            for idx, (user_id, _) in enumerate(sorted_users):
                if idx < len(sign_up_discord_ids):
                    if idx < len(sign_up_discord_ids):
                        discord_id_by_user_id[user_id] = sign_up_discord_ids[idx]
                    
                    if isinstance(sign_up_display_names[idx], str):
                        discord_name_by_user_id[user_id] = sign_up_display_names[idx]
                    elif isinstance(sign_up_display_names[idx], dict) and 'name' in sign_up_display_names[idx]:
                        # Handle dictionary format
                        discord_name_by_user_id[user_id] = sign_up_display_names[idx]['name']
            
            # Dictionary to store MagicProTools links for each Discord ID
            magicprotools_links = {}
            
            for idx, (user_id, user_data) in enumerate(sorted_users):
                user_name = user_data["userName"]
                
                # Get Discord display name if available
                discord_name = discord_name_by_user_id.get(user_id)
                discord_id = discord_id_by_user_id.get(user_id)
                
                # Add team color emoji based on player position
                # Odd positions (0, 2, 4...) are red team, even positions (1, 3, 5...) are blue team
                team_emoji = "🔴" if idx % 2 == 0 else "🔵"
                
                # Get win-loss record if available
                record_str = ""
                trophy_emoji = ""
                if discord_id and discord_id in player_records:
                    record = player_records[discord_id]
                    record_str = f" ({record['wins']}-{record['losses']})"
                    # Add trophy emoji if they have 3 wins
                    if record['wins'] == 3:
                        trophy_emoji = "🏆 "
                
                # Format the name with team emoji, trophy, and record
                display_name = f"{team_emoji} {trophy_emoji}{user_name}"
                if discord_name:
                    display_name = f"{team_emoji} {user_name} - {discord_name}{record_str} {trophy_emoji}"
                
                # Get paths for MagicProTools
                folder_path = f"draft_logs/{folder}/{session_id}"
                filename = f"DraftLog_{user_id}.txt"
                
                # Get the URLs from the DigitalOcean helper
                txt_key = f"{folder_path}/{filename}"
                txt_url = do_helper.get_public_url(txt_key)
                # Fallback: Generate import URL

                
                # Try to get or generate URL from MagicProTools helper
                self.logger.info(f"Attempting to get MagicProTools URL for user {user_name} (ID: {user_id})")
                try:
                    # Try to get a direct URL from MagicProTools API
                    self.logger.debug(f"Calling mpt_helper.submit_to_api for user {user_name}")
                    direct_mpt_url = await self.mpt_helper.submit_to_api(user_id, draft_data)
                    
                    if direct_mpt_url:
                        self.logger.info(f"SUCCESS: Got direct MagicProTools URL for {user_name}: {direct_mpt_url}")
                        # If API call successful, use the direct URL
                        final_mpt_url = direct_mpt_url
                        embed.add_field(
                            name=display_name,
                            value=f"[View on MagicProTools]({direct_mpt_url})",
                            inline=False
                        )
                        
                        # Get Discord ID and store the link in our dictionary
                        if discord_id:
                            magicprotools_links[discord_id] = {
                                "name": discord_name_by_user_id.get(user_id, user_name),
                                "link": direct_mpt_url
                            }
                            self.logger.debug(f"Stored MagicProTools link for Discord user {discord_id}")
                        else:
                            self.logger.warning(f"No Discord ID mapping found for user {user_name} (ID: {user_id})")
                        
                        self.logger.debug(f"Using DIRECT URL for {user_name} - skipping fallback code")
                        continue
                    else:
                        self.logger.warning(f"API call succeeded but returned no URL for user {user_name} - falling back to import URL")
                except Exception as e:
                    self.logger.error(f"ERROR submitting to MagicProTools API for {user_name}: {str(e)}")
                    self.logger.debug(f"Exception details for {user_name}: {repr(e)}")
                
                # Fallback: Add field with raw log link and import link (only executed if API method failed or didn't return a URL)
                mpt_url = f"https://magicprotools.com/draft/import?url={urllib.parse.quote(txt_url)}"
                final_mpt_url = mpt_url  # Default to import URL                self.logger.info(f"Using FALLBACK import URL for {user_name}: {mpt_url}")
                embed.add_field(
                    name=display_name,
                    value=f"[Import to MagicProTools]({mpt_url})",
                    inline=False
                )
                
                # Get Discord ID and store the link in our dictionary
                if discord_id:
                    magicprotools_links[discord_id] = {
                        "name": discord_name_by_user_id.get(user_id, user_name),
                        "link": final_mpt_url
                    }
                    self.logger.debug(f"Stored fallback MagicProTools link for Discord user {discord_id}")
                else:
                    self.logger.warning(f"No Discord ID mapping found for user {user_name} during fallback (ID: {user_id})")
            
            # Update the database with the MagicProTools links
            if magicprotools_links and draft_session:
                try:
                    draft_session.magicprotools_links = magicprotools_links
                    session.add(draft_session)  
                    await session.commit()
                    self.logger.info(f"Updated DraftSession with MagicProTools links for {len(magicprotools_links)} users")
                except Exception as e:
                    self.logger.error(f"Error saving MagicProTools links to database: {e}")
            
            return embed
        except Exception as e:
            self.logger.error(f"Error generating Discord embed: {e}")
            # Return a basic embed if there's an error
            return discord.Embed(
                title=f"Draft Log: {draft_data.get('sessionID')}",
                description="Error generating MagicProTools links. Check logs for details.",
                color=0xFF0000  # Red color
            )

    def get_pack_first_picks(self, draft_data, user_id):
        """Extract the first pick card name for each pack for a specific user."""
        try:
            # Use the MagicProtoolsHelper to get the first picks
            return self.mpt_helper.get_pack_first_picks(draft_data, user_id)
        except Exception as e:
            # In case of any error, return empty result
            self.logger.error(f"Error getting first picks: {e}")
            return {}

    async def fetch_draft_info(self):
        """Load draft channel and other info from database"""
        try:
            # Get draft session info
            draft_session = await DraftSession.get_by_session_id(self.session_id)
            if draft_session:
                self.draft_channel_id = draft_session.draft_channel_id
                self.session_type = draft_session.session_type or "team"
                self.guild_id = draft_session.guild_id
                
                # Calculate expected user count from sign_ups
                if draft_session.sign_ups:
                    self.expected_user_count = len(draft_session.sign_ups)
                    self.logger.info(f"Expected user count from database: {self.expected_user_count}")
                    
                    # Log the expected users for comparison
                    self.logger.info(f"Expected users: {list(draft_session.sign_ups.values())}")
                    
                    # Also log the current users in the session
                    non_bot_users = [u.get('userName') for u in self.session_users if u.get('userName') != 'DraftBot']
                    self.logger.info(f"Current non-bot users: {non_bot_users}")
                    
                    return True
                else:
                    self.logger.warning("No sign_ups found in database, falling back to session users count")
                    non_bot_users = [u for u in self.session_users if u.get('userName') != 'DraftBot']
                    self.expected_user_count = len(non_bot_users)
                    self.logger.info(f"Expected user count from current users: {self.expected_user_count}")
                    return True
            return False
        except Exception as e:
            self.logger.error(f"Error fetching draft info: {e}")
            return False

    async def initiate_ready_check(self, bot):
        """Initiates the ready check process"""
        if self.ready_check_active:
            return False

        # Make sure we have channel info
        if not await self.fetch_draft_info():
            self.logger.error("Failed to fetch draft channel info")
            return False

        # Check for missing users first
        draft_session = await DraftSession.get_by_session_id(self.session_id)
        if draft_session and draft_session.sign_ups:
            user_status = self._compute_user_status(draft_session.sign_ups)

            if user_status['missing']:
                # Users are missing, can't start ready check
                channel = await self._get_draft_channel()
                if channel:
                    missing_users_str = ", ".join(user_status['missing'])
                    await channel.send(
                        f"⚠️ **Cannot start ready check**\n"
                        f"Missing users: {missing_users_str}\n"
                        f"These players need to join the Draftmancer session first."
                    )
                return False

        seating_ok, seating_message = await self.verify_seating_order()

        if seating_ok:
            self.logger.info("Seating verification and reset succeeded")
        else:
            self.logger.error(f"Seating verification failed: {seating_message}")
            # Post failure message in Discord
            channel = await self._get_draft_channel()
            if channel:
                await channel.send(
                    f"⚠️ **Seating order verification failed!** {seating_message}\n\n"
                    f"Use `/mutiny` to take control and fix the seating manually, "
                    f"or try `/ready` again when all players have reconnected."
                )

        self.ready_check_active = True
        self.ready_users.clear()
        self.post_timeout_ready_users.clear()

        try:
            # Count expected participants
            non_bot_users = [u for u in self.session_users if u.get('userName') != 'DraftBot']
            total_users = len(non_bot_users)

            # If we couldn't get expected count from database, use current users
            if self.expected_user_count is None or self.expected_user_count == 0:
                self.expected_user_count = total_users

            # Get channel for ready check message
            channel = await self._get_draft_channel()
            if not channel:
                self.ready_check_active = False
                return False
            
            # Calculate the timestamp 90 seconds from now
            future_time = datetime.now() + timedelta(seconds=90)
            timeout = int(future_time.timestamp())

            message = await channel.send(
                f"🔔🔔 Seating order set. Draftmancer Readycheck in progress: 0/{self.expected_user_count} ready.\n"
                f"{READY_CHECK_INSTRUCTIONS}"
            )
            # Store initial message ID safely
            self.ready_check_message_id = str(message.id)
            await self.update_draft_session_field('ready_check_message_id', str(message.id))

            # Store the timeout message ID
            timeout_message = await channel.send(f"Readycheck will timeout <t:{timeout}:R>")
            self.timeout_message_id = str(timeout_message.id)

            # Send DM notifications to users who have opted in
            if draft_session and draft_session.sign_ups:
                await send_ready_check_dms(
                    bot_or_client=bot,
                    draft_session=draft_session,
                    guild_id=str(channel.guild.id),
                    channel_id=str(channel.id),
                    channel_name=channel.name,
                    guild_name=channel.guild.name
                )

            # Start timeout timer
            self.ready_check_timer = asyncio.create_task(self.ready_check_timeout(90, bot))
            
            # Emit ready check to Draftmancer
            await self.socket_client.emit('readyCheck')
            
            self.logger.info(f"Ready check initiated for session {self.session_id}")
            return True
            
        except Exception as e:
            self.logger.exception(f"Error initiating ready check: {e}")
            self.ready_check_active = False
            return False

    async def verify_seating_order(self):
        """
        Verifies the seating order by forcing a reset to ensure correctness.
        Returns a tuple of (success, message)
        """
        self.logger.info("=== SEATING VERIFICATION START ===")
        start_time = datetime.now()
        
        # First, ensure we have the desired seating order
        if not self.desired_seating_order:
            self.logger.info("No desired seating order stored, fetching from database")
            try:
                draft_session = await DraftSession.get_by_session_id(self.session_id)
                if draft_session and draft_session.sign_ups:
                    self.desired_seating_order = list(draft_session.sign_ups.values())
                    self.logger.info(f"Fetched desired seating order: {self.desired_seating_order}")
                else:
                    self.logger.warning("No sign_ups found in database")
                    return False, "No seating order defined in database"
            except Exception as e:
                self.logger.exception(f"Error fetching draft session: {e}")
                return False, f"Error fetching draft data: {str(e)}"
        else:
            self.logger.info(f"Using existing desired seating order: {self.desired_seating_order}")
        
        try:
            # Map usernames to user IDs
            username_to_userid = {}
            for user in self.session_users:
                if user.get('userName') != 'DraftBot':  # Exclude bot
                    username = user.get('userName')
                    user_id = user.get('userID')
                    if username and user_id:
                        username_to_userid[username] = user_id
            
            # Check if all expected users are present
            missing_users = []
            for username in self.desired_seating_order:
                if username not in username_to_userid:
                    missing_users.append(username)
            
            self.logger.info(f"Current session users: {list(username_to_userid.keys())}")
            
            if missing_users:
                self.logger.warning(f"Missing users: {missing_users}")
                if len(missing_users) > len(self.desired_seating_order) // 2:
                    self.logger.error("Too many users missing")
                    return False, f"Too many users missing from session: {', '.join(missing_users)}"
            
            # Instead of trying to get the current order (which times out),
            # always reset the seating order to ensure it's correct
            self.logger.info("Resetting seating order to ensure correctness")
            success, remaining_missing = await self.set_seating_order(self.desired_seating_order)
            
            if success:
                self.logger.info("Successfully reset seating order")
                return True, "Seating order verified and reset"
            else:
                self.logger.error(f"Failed to reset seating order: {remaining_missing}")
                return False, f"Failed to verify seating order. Missing users: {', '.join(remaining_missing)}"
            
        except Exception as e:
            elapsed = (datetime.now() - start_time).total_seconds()
            self.logger.exception(f"Error in seating verification after {elapsed:.2f}s: {e}")
            return False, f"Error checking seating: {str(e)}"
        
        finally:
            elapsed = (datetime.now() - start_time).total_seconds()
            self.logger.info(f"=== SEATING VERIFICATION END (took {elapsed:.2f}s) ===")
            
    async def handle_user_ready_update(self, userID, readyState):
        """Handle updates when a user changes their ready state"""
        self.logger.info(f"Ready state update received: User {userID} set to state {readyState}")
        
        # Check if user is ready - supporting multiple formats
        is_ready = readyState == 1 or readyState == "Ready" or str(readyState).lower() == "ready"
        
        # Get bot reference for actions
        bot = get_bot()
        if not bot:
            self.logger.warning("Could not get bot instance for ready state handling")
            return
        
        if self.ready_check_active:
            # Normal flow during active ready check
            if is_ready:
                self.ready_users.add(userID)
                self.logger.info(f"User {userID} marked as READY during active check")
            else:
                if userID in self.ready_users:
                    self.ready_users.remove(userID)
                    self.logger.info(f"User {userID} marked as NOT READY during active check")
            
            # Update the ready check message
            await self.update_ready_check_message(bot)
            
            # Check if all users are ready
            non_bot_users = [u for u in self.session_users if u.get('userName') != 'DraftBot']
            self.logger.info(f"Ready users: {len(self.ready_users)}/{len(non_bot_users)} (expected: {self.expected_user_count})")
            
            if len(self.ready_users) >= self.expected_user_count:
                self.logger.info(f"All users ready! ({len(self.ready_users)}/{self.expected_user_count})")
                await self.complete_ready_check()
            else:
                self.logger.info(f"Still waiting for {self.expected_user_count - len(self.ready_users)} more users")
        
        else:
            # After timeout behavior - track readiness and potentially start new check
            if is_ready:
                self.post_timeout_ready_users.add(userID)
                self.logger.info(f"User {userID} marked as READY after timeout")
            else:
                if userID in self.post_timeout_ready_users:
                    self.post_timeout_ready_users.remove(userID)
                    self.logger.info(f"User {userID} marked as NOT READY after timeout")
            
            # Check if all users are now ready after timeout
            non_bot_users = [u for u in self.session_users if u.get('userName') != 'DraftBot']
            self.logger.info(f"Post-timeout ready users: {len(self.post_timeout_ready_users)}/{len(non_bot_users)} (expected: {self.expected_user_count})")
            
            if len(self.post_timeout_ready_users) >= self.expected_user_count:
                self.logger.info(f"All users now ready after timeout! Starting new ready check")
                
                # Reset post-timeout tracking
                self.post_timeout_ready_users.clear()
                
                # Start a new ready check
                await self.initiate_ready_check(bot)
                    
    async def update_ready_check_message(self, bot):
        """Update the ready check message with current count"""
        if not self.ready_check_message_id:
            self.logger.error("Cannot update message - missing message ID")
            return False

        channel = await self._get_draft_channel()
        if not channel:
            return False

        ready_count = len(self.ready_users)
        new_content = (
            f"Seating order set. Draftmancer Readycheck in progress: {ready_count}/{self.expected_user_count} ready.\n"
            f"{READY_CHECK_INSTRUCTIONS}"
        )

        self.logger.info(f"Updating ready check message to show {ready_count}/{self.expected_user_count} ready")
        message = await self._update_or_send_message(channel, self.ready_check_message_id, content=new_content)
        return message is not None

    async def ready_check_timeout(self, seconds, bot):
        """Handles timeout for the ready check"""
        try:
            await asyncio.sleep(seconds)

            # Delete the timeout message after the time expires
            channel = await self._get_draft_channel()
            if channel and self.timeout_message_id:
                await self._safe_delete_message(channel, self.timeout_message_id)
                self.logger.info("Timeout message successfully deleted")
                self.timeout_message_id = None

            # If we reach here, the ready check timed out
            if self.ready_check_active:
                self.logger.info("Ready check timed out, but continuing to track readiness")
                self.ready_check_active = False

                # Initialize post-timeout tracking with currently ready users
                self.post_timeout_ready_users = self.ready_users.copy()

                # Identify which users weren't ready at timeout
                missing_users = [
                    user.get('userName') for user in self.session_users
                    if user.get('userName') != 'DraftBot' and user.get('userID') not in self.ready_users
                ]

                # Format missing users for display
                missing_text = f"\nWaiting for: **{', '.join(missing_users)}**" if missing_users else ""

                # Prepare and send the timeout message
                timeout_message = (
                    f"⚠️ **Ready check failed!** Timed out after {seconds} seconds.{missing_text}\n"
                    f"A new ready check will start automatically when all players are present."
                    f"{READY_CHECK_INSTRUCTIONS}"
                )

                if channel and self.ready_check_message_id:
                    await self._update_or_send_message(channel, self.ready_check_message_id, content=timeout_message)
                    self.logger.info("Ready check message updated for timeout")

                # Reset message ID but keep tracking readiness
                self.ready_check_message_id = None
                self.ready_users.clear()

        except asyncio.CancelledError:
            # Expected if the timer is cancelled when all users become ready
            pass

    async def complete_ready_check(self):
        """Called when all users are ready"""
        if not self.ready_check_active:
            self.logger.info("complete_ready_check called but no active ready check")
            return

        self.logger.info("All users ready! Completing ready check")
        self.ready_check_active = False

        # Cancel the timeout timer
        if self.ready_check_timer:
            self.logger.info("Cancelling ready check timeout timer")
            self.ready_check_timer.cancel()
            self.ready_check_timer = None

        # Delete the timeout message
        channel = await self._get_draft_channel()
        if channel and self.timeout_message_id:
            await self._safe_delete_message(channel, self.timeout_message_id)
            self.logger.info("Timeout message deleted on successful ready check")
            self.timeout_message_id = None

        try:
            if channel:
                self.logger.info("Sending ready success message")
                await channel.send("🎉 All drafters ready! Draft starting in 5 seconds...")

            # Wait before starting
            self.logger.info("Waiting 5 seconds before starting draft")
            await asyncio.sleep(5)

            # Start the draft
            self.logger.info("Starting draft")
            await self.start_draft()
        except Exception as e:
            self.logger.exception(f"Error completing ready check: {e}")

    async def invalidate_ready_check(self, user_name):
        """
        Invalidates the current ready check when a user joins or leaves during the ready check.
        This only cancels the current ready check without trying to restart it.
        The update_status_message_after_user_change method will handle restarting when all users are present.
        """
        self.logger.info(f"Invalidating ready check due to user change from {user_name}")

        # Cancel the current ready check timer if it exists
        if self.ready_check_timer:
            try:
                self.ready_check_timer.cancel()
                self.ready_check_timer = None
            except Exception as e:
                self.logger.error(f"Error cancelling ready check timer during invalidation: {e}")

        # Prepare the failure message
        failure_message = (
            f"⚠️ **Ready check failed!** User {user_name} joined or left during the ready check.\n"
            f"A new ready check will start automatically when all players are present."
        )

        # Update or send the failure message
        channel = await self._get_draft_channel()
        if channel:
            await self._update_or_send_message(channel, self.ready_check_message_id, content=failure_message)
            self.logger.info("Ready check failure message sent/updated")

        # Reset the ready check state
        self.ready_check_active = False
        self.ready_users.clear()
        self.post_timeout_ready_users.clear()
        self.ready_check_message_id = None

        # Also reset the seating order flag so it will be re-verified
        self.seating_order_set = False
        self.seating_attempts = 0  # Reset attempt counter

        # Note: We don't try to restart the ready check here.
        # The update_status_message_after_user_change method will detect when
        # all expected users are present and call check_session_stage_and_organize,
        # which will verify seating and automatically start a new ready check
            
    async def start_draft(self):
        """Start the draft after successful ready check"""
        try:
            # Define a callback handler for the response
            callback_future = asyncio.Future()
            
            def ack_callback(response):
                self.logger.info(f"Start draft response: {response}")
                # Set the future's result
                callback_future.set_result(response)
            
            # Emit the start draft event
            await self.socket_client.emit('startDraft', callback=ack_callback)
            self.logger.info("Draft start requested")
            
            # Wait for the response
            try:
                response = await asyncio.wait_for(callback_future, timeout=10)
                if response and 'error' in response:
                    self.logger.error(f"Error starting draft: {response['error']}")
                    
                    # Notify about the error
                    if hasattr(self, 'bot') and self.bot:
                        channel = self.bot.get_channel(int(self.draft_channel_id))
                        if channel:
                            await channel.send(f"Error starting draft: {response['error']}")
                else:
                    self.drafting = True
                    self.logger.info("Draft started successfully")
            except asyncio.TimeoutError:
                self.logger.warning("Timeout waiting for draft start response")
                
        except Exception as e:
            self.logger.exception(f"Error starting draft: {e}")
            
    async def check_session_stage_and_organize(self):
        """Check database for session stage and organize seating if appropriate"""
        if self.seating_order_set or self.seating_attempts >= 4:
            return  # Already set or max attempts reached
            
        try:
            # Fetch draft session from database
            draft_session = await DraftSession.get_by_session_id(self.session_id)
            
            if not draft_session:
                self.logger.warning(f"No draft session found for session_id: {self.session_id}")
                return
                
            # Check if session stage is "teams"
            if draft_session.session_stage:
                self.logger.info("Session stage is 'teams', checking for seating organization")
                
                # Get sign_ups from the database
                sign_ups = draft_session.sign_ups
                if not sign_ups:
                    self.logger.warning("No sign-ups found in database")
                    return
                    
                # Expected user count is exactly the number of sign-ups (bot is spectator)
                self.expected_user_count = len(sign_ups)
                
                # Desired seating order is the values from sign_ups dictionary
                self.desired_seating_order = list(sign_ups.values())
                
                # Log our intentions
                self.logger.info(f"Expected players: {len(sign_ups)}, Current non-bot users: {self.users_count}")
                self.logger.info(f"Desired seating order: {self.desired_seating_order}")
                
                # Get status message ID from database if we don't have it
                if not hasattr(self, 'status_message_id') or not self.status_message_id:
                    self.status_message_id = draft_session.status_message_id

                # Make sure draft_channel_id is properly set - fetch again if needed
                if not self.draft_channel_id and draft_session.draft_channel_id:
                    self.draft_channel_id = draft_session.draft_channel_id
                    self.logger.info(f"Updated draft_channel_id from database: {self.draft_channel_id}")

                # Compute user status using helper
                user_status = self._compute_user_status(sign_ups)

                # Update our session status
                self.session_status = {
                    'present_users': sorted(list(user_status['present'])),
                    'missing_users': sorted(list(user_status['missing'])),
                    'unexpected_users': sorted(list(user_status['unexpected'])),
                    'updated_at': datetime.now().strftime('%H:%M:%S')
                }

                # Send/update status message to Discord channel if available
                channel = await self._get_draft_channel()
                if channel:
                    self.logger.info(f"Found channel: #{channel.name}")
                    await self.send_session_status_message(channel)
                
                # Check if we have all expected users to attempt setting the order
                if not user_status['missing'] and self.users_count >= self.expected_user_count:
                    if self.removing_unexpected_user:
                        self.logger.info("All expected users present, but we're removing an unexpected user. Delaying seating attempt.")
                    else:
                        self.logger.info("All expected users are present, attempting to set seating order")
                        await self.attempt_seating_order(self.desired_seating_order)
                else:
                    self.logger.info(f"Not all users present. Missing: {user_status['missing']}")
                    # Don't attempt seating until all users are present
                    
        except Exception as e:
            self.logger.exception(f"Error checking session stage: {e}")

    async def attempt_seating_order(self, desired_seating_order):
        """Attempt to set the seating order"""
        async with self._seating_lock:
            if self.seating_order_set or self.seating_attempts >= 4:
                return

            self.seating_attempts += 1
            self.logger.info(f"Attempt {self.seating_attempts}: Setting seating order with {self.users_count} users")

            success, missing_users = await self.set_seating_order(desired_seating_order)

            if success:
                self.logger.success(f"Successfully set seating order!")
                self.seating_order_set = True

                # Update status message with success
                self.session_status['status'] = 'seating_success'
                self.session_status['updated_at'] = datetime.now().strftime('%H:%M:%S')

                channel = await self._get_draft_channel()
                if channel and self.status_message_id:
                    new_content = self.format_status_message(self.session_status)
                    new_content += "\n\n✅ **Seating order set successfully! Starting ready check...**"
                    await self._update_or_send_message(channel, self.status_message_id, content=new_content)

                # Automatically initiate the first ready check after seating is set
                await asyncio.sleep(1)  # Brief pause to ensure everything is settled

                bot = get_bot()
                if bot:
                    self.logger.info("Seating order set successfully, initiating automatic ready check")
                    await self.initiate_ready_check(bot)
                else:
                    self.logger.error("Could not get bot instance for automatic ready check")
            else:
                self.logger.warning(f"Failed to set seating order, missing users: {missing_users}")

                # Update status message with failure
                self.session_status['status'] = 'seating_failed'
                self.session_status['missing_users'] = missing_users
                self.session_status['updated_at'] = datetime.now().strftime('%H:%M:%S')

                channel = await self._get_draft_channel()
                if channel and self.status_message_id:
                    new_content = self.format_status_message(self.session_status)
                    missing_users_str = ", ".join(missing_users)
                    new_content += f"\n\n❌ **Failed to set seating order. Missing users: {missing_users_str}**\n"
                    new_content += f"These players need to join the Draftmancer session."
                    await self._update_or_send_message(channel, self.status_message_id, content=new_content)

                    # Also send a separate notification for visibility
                    await channel.send(
                        f"⚠️ **Seating order could not be set**\n"
                        f"Missing users: {missing_users_str}\n"
                        f"These players need to join the Draftmancer session."
                    )

                if self.seating_attempts >= 4:
                    self.logger.error(f"Failed to set seating order after {self.seating_attempts} attempts")
                    await self.notify_seating_failure(missing_users)

    @exponential_backoff(max_retries=10, base_delay=1)
    async def set_seating_order(self, desired_username_order):
        """
        Sets the seating order for the draft based on usernames.
        Bot is a spectator and not included in seating order.
        """
        if not self.socket_client.connected:
            self.logger.error("Cannot set seating order - socket not connected")
            return False, ["Connection lost"]
            
        try:
            # Always print out the actual session_users to debug
            self.logger.info(f"Current session users: {[user.get('userName') for user in self.session_users]}")
            
            # Find the DraftBot user to exclude from seating
            bot_id = None
            for user in self.session_users:
                if user.get('userName') == 'DraftBot':
                    bot_id = user.get('userID')
                    self.logger.info(f"Found DraftBot ID: {bot_id} (will be excluded from seating)")
                    break
            
            # Create mapping of usernames to userIDs, excluding the bot
            username_to_userid = {}
            for user in self.session_users:
                user_id = user.get('userID')
                username = user.get('userName')
                
                if user_id != bot_id and username:  # Exclude bot from mapping
                    username_to_userid[username] = user_id
                    self.logger.debug(f"Mapped {username} to {user_id}")
            
            # Convert the username order to userID order
            user_id_order = []
            missing_users = []
            
            for username in desired_username_order:
                if username in username_to_userid:
                    user_id_order.append(username_to_userid[username])
                    self.logger.debug(f"Added {username} to seating order")
                else:
                    missing_users.append(username)
                    self.logger.warning(f"Username '{username}' not found in session")
            
            if not user_id_order:
                self.logger.error("No valid userIDs found for the provided usernames")
                return False, desired_username_order
                
            # Only proceed if all users are present
            if missing_users:
                self.logger.warning(f"Cannot set seating order - missing users: {missing_users}")
                return False, missing_users
                
            # Set the seating order using userIDs (bot not included)
            self.logger.info(f"Setting seating order: {user_id_order}")
            await self.socket_client.emit('setSeating', user_id_order)
            
            # All users are present, so we succeeded
            return True, []
            
        except Exception as e:
            self.logger.error(f"Error while setting seating order: {e}")
            self.logger.exception("Full exception details:")
            return False, [str(e)]
        
    async def disconnect_after_delay(self, delay_seconds):
        """
        Disconnects from the session after a delay to ensure commands have been processed.
        Ensures proper cleanup and logging.
        """
        await asyncio.sleep(delay_seconds)
        await self.disconnect_safely()

    async def mark_draft_cancelled(self):
        """Mark that the draft is being cancelled manually, skip log collection"""
        self.logger.info(f"Marking draft {self.draft_id} as manually cancelled")
        self.draft_cancelled = True
        # Set these flags to prevent log collection attempts for cancelled drafts
        self.logs_collection_attempted = True
        self.logs_collection_success = False
        
    async def disconnect_safely(self):
        """
        Central method to handle disconnection safely and consistently.
        Ensures proper sequence of operations: 
        1. Set owner as player first
        2. Transfer ownership 
        3. Disconnect
        """
        if not self.socket_client.connected:
            return
        
        self._should_disconnect = True
        try:
            try:
                self.logger.info("Setting owner as player before transferring ownership")
                await self.socket_client.emit('setOwnerIsPlayer', True)
                await asyncio.sleep(1)  # Increased delay to ensure the setting is processed
            except Exception as e:
                self.logger.warning(f"Failed to set owner as player: {e}")
            
            # Disconnect
            await self.socket_client.disconnect()
            self.logger.info("Disconnected successfully")
            
            # Remove from active managers registry
            if self.session_id in ACTIVE_MANAGERS:
                del ACTIVE_MANAGERS[self.session_id]
                self.logger.info(f"Removed manager for session {self.session_id} from active managers registry")
        except Exception as e:
            self.logger.exception(f"Error during disconnect: {e}")
            
    @classmethod
    def get_active_manager(cls, session_id: str):
        """
        Get an active manager instance for a session if it exists
        
        Args:
            session_id: The session ID to look up
            
        Returns:
            The DraftSetupManager instance if found, None otherwise
        """
        return ACTIVE_MANAGERS.get(session_id)
    
    async def _handle_victory_aware_disconnect(self):
        """
        Handle disconnect with victory detection awareness.
        
        Waits for victory detection to complete before disconnecting,
        but includes fallback timer to prevent indefinite connections.
        """
        try:
            self.logger.debug(f"Starting victory-aware disconnect with {VICTORY_CHECK_TIMEOUT}s timeout")
            await asyncio.wait_for(self._wait_for_victory(), timeout=VICTORY_CHECK_TIMEOUT)
            self.logger.info("Victory detected, proceeding with disconnect")
        except asyncio.TimeoutError:
            self.logger.info(f"Victory check timeout ({VICTORY_CHECK_TIMEOUT}s) reached, disconnecting without victory confirmation")
        except Exception as e:
            self.logger.exception(f"Unexpected error during victory-aware disconnect: {e}")
        finally:
            # Always ensure disconnect happens
            self._should_disconnect = True
            await self.disconnect_safely()
    
    async def _wait_for_victory(self):
        """
        Continuously check for victory until detected.
        
        This method will run until victory is detected or cancelled by timeout.
        """
        # Initial short delay for immediate victory detection
        self.logger.debug(f"Initial {VICTORY_CHECK_INITIAL_DELAY}s delay for immediate victory detection")
        await asyncio.sleep(VICTORY_CHECK_INITIAL_DELAY)
        
        check_count = 0
        while True:
            victory_detected = await self._check_victory_status()
            
            if victory_detected:
                return  # Victory found, exit the loop
            
            check_count += 1
            self.logger.debug(f"Victory not yet detected (check #{check_count}), waiting {VICTORY_CHECK_INTERVAL}s")
            await asyncio.sleep(VICTORY_CHECK_INTERVAL)
    
    async def _check_victory_status(self):
        """
        Check if victory has been detected for this draft session.
        
        Returns:
            bool: True if victory detected, False otherwise
        """
        try:
            async with AsyncSessionLocal() as db_session:
                # Check if victory message has been posted
                stmt = select(DraftSession).where(DraftSession.session_id == self.session_id)
                draft_session = await db_session.scalar(stmt)
                
                if not draft_session:
                    self.logger.warning(f"Draft session {self.session_id} not found in database")
                    return False
                
                # Victory is considered detected if any victory message ID is set
                victory_fields = [
                    'victory_message_id_draft_chat',
                    'victory_message_id_lobby',
                    'draw_message_id_draft_chat', 
                    'draw_message_id_lobby'
                ]
                
                for field in victory_fields:
                    if hasattr(draft_session, field) and getattr(draft_session, field) is not None:
                        self.logger.debug(f"Victory detected via {field}")
                        return True
                
                return False
                
        except ImportError as e:
            self.logger.error(f"Import error checking victory status - database module unavailable: {e}")
            # Import errors suggest configuration issue, continue checking
            return False
        except Exception as e:
            # Check for specific database-related errors that might indicate temporary issues
            error_message = str(e).lower()
            if any(db_error in error_message for db_error in ['database is locked', 'connection', 'timeout']):
                self.logger.warning(f"Temporary database error checking victory status: {e}")
                # For temporary DB issues, continue checking (don't assume victory)
                return False
            else:
                # For other unexpected errors, log and continue checking
                self.logger.exception(f"Unexpected error checking victory status: {e}")
                return False
    
    async def mark_draft_cancelled(self):
        """Mark that the draft is being cancelled manually"""
        self.logger.info(f"Marking draft {self.draft_id} as manually cancelled")
        self.draft_cancelled = True
        
    async def notify_seating_failure(self, missing_users):
        """
        Notifies about failure to set the seating order.
        
        Args:
            missing_users: List of usernames that couldn't be matched
        """
        self.logger.error(f"Seating order failed: Could not match users {missing_users}")
        
        # Here we'd ideally send a message back to Discord
        # Since we don't have a direct connection back to the Discord bot, we could:
        # 1. Update a field in the database to indicate failure
        # 2. Create a webhook that the Discord bot checks
        # 3. Send a direct HTTP request to a Discord webhook URL
        
        try:
            # Update database to indicate failure
            draft_session = await DraftSession.get_by_session_id(self.draft_id)
            if draft_session:
                await draft_session.update(
                    data_received=True,
                    draft_data={
                        "seating_failed": True,
                        "missing_users": missing_users,
                        "timestamp": datetime.now().isoformat()
                    }
                )
                self.logger.info("Updated database with seating failure information")
        except Exception as e:
            self.logger.exception(f"Failed to update database with seating failure: {e}")

    @exponential_backoff(max_retries=10, base_delay=1)
    async def update_draft_settings(self):
        if not self.socket_client.connected:
            self.logger.error("Cannot update settings - socket not connected")
            return False
            
        try:
            # Send each setting individually
            self.logger.debug("Updating draft settings...")
            await self.socket_client.emit('setColorBalance', False)
            await self.socket_client.emit('setMaxPlayers', 10)
            await self.socket_client.emit('setDraftLogUnlockTimer', 180)
            await self.socket_client.emit('setDraftLogRecipients', "delayed")
            await self.socket_client.emit('setPersonalLogs', True)
            await self.socket_client.emit('teamDraft', True)  # Added teamDraft setting
            await self.socket_client.emit('setPickTimer', 60)
            await self.socket_client.emit('setOwnerIsPlayer', False)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error during settings update: {e}")
            self.logger.exception("Full exception details:")
            return False

    @exponential_backoff(max_retries=10, base_delay=1)
    async def import_cube(self, allow_regeneration: bool = True):
        """
        Import cube from CubeCobra to Draftmancer.

        If the bot is no longer the session owner:
        - If no users have joined yet, regenerate the session and retry
        - If users have already joined, notify them and stop managing

        Args:
            allow_regeneration: If True and we get an ownership error before users join,
                              regenerate the session and retry once.
        """
        try:
            import_data = {
                "service": "Cube Cobra",
                "cubeID": self.cube_id,
                "matchVersions": True
            }

            # Create a Future to wait for the callback
            # Result is a tuple: (success, is_ownership_error)
            future = asyncio.Future()

            def ack(response):
                if 'error' in response:
                    error_info = response['error']
                    self.logger.error(f"Import cube error: {error_info}")

                    # Check if this is the "Must be session owner" error
                    # The error format is: {'icon': 'error', 'title': 'Unautorized', 'text': 'Must be session owner.'}
                    is_ownership_error = False
                    if isinstance(error_info, dict):
                        error_text = error_info.get('text', '')
                        error_title = error_info.get('title', '')
                        if 'session owner' in error_text.lower() or 'unautorized' in error_title.lower() or 'unauthorized' in error_title.lower():
                            is_ownership_error = True

                    future.set_result((False, is_ownership_error))
                else:
                    self.logger.info("Cube import acknowledged")
                    self.cube_imported = True
                    future.set_result((True, False))

            await self.socket_client.emit('importCube', import_data, callback=ack)
            self.logger.info(f"Sent cube import request for {self.cube_id}")

            # Wait for the callback to complete
            success, is_ownership_error = await future

            # Handle ownership error
            if not success and is_ownership_error:
                # Check if users have already joined the Draftmancer session
                users_have_joined = self.users_count > 0 or len(self.session_users) > 0

                if users_have_joined:
                    # Users already have the link and may have joined - notify and stop
                    self.logger.warning("Bot lost ownership after users joined, notifying and stopping management")
                    await self._notify_bot_no_longer_managing()
                    if self.socket_client.connected:
                        await self.socket_client.disconnect()
                    return False
                elif allow_regeneration:
                    # No users yet - regenerate session so we can provide a valid link
                    self.logger.info("Bot lost ownership before users joined, regenerating session...")

                    if await self.regenerate_draft_session():
                        self.logger.info("Session regenerated successfully, reconnecting...")

                        if await self.connect_to_new_session():
                            self.logger.info("Reconnected to new session, retrying cube import...")
                            # Retry import once (with regeneration disabled to prevent infinite loop)
                            return await self.import_cube(allow_regeneration=False)
                        else:
                            self.logger.error("Failed to connect to new session after regeneration")
                            return False
                    else:
                        self.logger.error("Failed to regenerate session")
                        return False
                else:
                    # Regeneration already attempted and failed
                    self.logger.error("Ownership error persists after regeneration attempt")
                    await self._notify_bot_no_longer_managing()
                    if self.socket_client.connected:
                        await self.socket_client.disconnect()
                    return False

            return success

        except Exception as e:
            self.logger.error(f"Fatal error during cube import: {e}")
            if self.socket_client.connected:
                await self.socket_client.disconnect()
            return False

    async def keep_connection_alive(self):
        """
        Main loop to keep connection alive and manage draft state.
        Now uses DraftSocketClient for robust connection handling.
        """
        self.logger.info(f"Starting connection management for draft {self.draft_id}")
        
        # Try initial connection
        websocket_url = get_draftmancer_websocket_url(self.draft_id)
        
        # First connection attempt
        if not await self.socket_client.connect_with_retry(websocket_url):
            self.logger.error("Initial connection failed after retries. Aborting.")
            return

        try:
            while True:
                # Check for disconnect conditions
                if self._should_disconnect:
                    await self.socket_client.disconnect()
                    break
                    
                # If disconnected, try to reconnect
                if not self.socket_client.connected:
                    self.logger.info("Connection lost, attempting to reconnect...")
                    reconnected = await self.socket_client.connect_with_retry(websocket_url)
                    
                    if not reconnected:
                        # If we can't reconnect, check if we've lost ownership
                        # This logic was in the original code, adapted here
                        self.logger.error("Failed to reconnect.")
                        if not self.session_users_received:
                             # logic for potentially regenerating session if no one joined
                             pass 
                             
                # Only perform actions if connected
                if self.socket_client.connected:
                    # Import cube if needed (uses proper import_cube with ownership handling)
                    if not self.cube_imported:
                        if not await self.import_cube():
                            self.logger.error("Cube import failed in keep_connection_alive loop")
                            # import_cube handles ownership errors internally
                            # If it returns False, we may have lost ownership or failed
                            continue

                    # Update settings if needed
                    if not self.settings_updated:
                        if await self.update_draft_settings():
                            self.settings_updated = True
                        else:
                            self.logger.warning("Failed to update draft settings, will retry")

                    try:
                        await self.socket_client.emit('getUsers')
                    except Exception as e:
                        self.logger.error(f"Error emitting getUsers: {e}")
                
                # Sleep before next iteration
                await asyncio.sleep(10)  # Regular check interval

                    
        except Exception as e:
            self.logger.exception(f"Fatal error in keep_connection_alive: {e}")
        finally:
            self._is_connecting = False
            # Only disconnect if requested
            if self._should_disconnect:
                await self.disconnect_safely()

    # NOTE: connect_with_retry is now handled by self.socket_client.connect_with_retry()

    async def manually_unlock_draft_logs(self):
        """
        Manually unlock draft logs for the currently connected Draftmancer session.
        """
        try:
            self.logger.info("Attempting to unlock draft logs for the connected Draftmancer session")
            
            # First, try to request the current draft log if we don't have it
            if not self.current_draft_log:
                self.logger.info("No draft log captured yet, attempting to request it")
                
                # Try to get the current draft log by requesting it
                callback_future = asyncio.Future()
                
                def on_draft_log_response(draft_log):
                    if draft_log:
                        self.logger.info("Received draft log from request")
                        self.current_draft_log = draft_log
                    callback_future.set_result(draft_log is not None)
                
                # Request the current draft log
                await self.socket_client.emit('getCurrentDraftLog', callback=on_draft_log_response)
                
                # Wait for response with timeout
                try:
                    success = await asyncio.wait_for(callback_future, timeout=5)
                    if not success:
                        self.logger.warning("Failed to get current draft log")
                except asyncio.TimeoutError:
                    self.logger.warning("Timeout waiting for draft log")
            
            # Now try to unlock the logs
            if self.current_draft_log:
                # Create a copy of the log to modify
                draft_log = self.current_draft_log.copy()
                
                # Set delayed to false to make it public
                draft_log['delayed'] = False
                
                # Emit the modified log
                self.logger.info(f"Sharing draft log with delayed=false")
                await self.socket_client.emit('shareDraftLog', draft_log)
                
                self.logger.info("Logs unlocked using captured draft log")
            else:
                # Fallback: try to create a minimal log with just the essential information
                self.logger.warning("No draft log available, further improvement required")
            
            # Continue with log collection as before
            if not self.logs_collection_attempted and not self.logs_collection_in_progress:
                asyncio.create_task(self.schedule_log_collection(60))
            
            return True
        except Exception as e:
            self.logger.error(f"Error unlocking draft logs: {e}")
            return False

    async def schedule_log_collection(self, delay_seconds):
        """Schedule log collection after a delay to ensure all data is available"""
        try:
            self.logger.info(f"Scheduling log collection in {delay_seconds} seconds")
            await asyncio.sleep(delay_seconds)
            await self.collect_draft_logs()
        except Exception as e:
            self.logger.exception(f"Error scheduling log collection: {e}")
                
    async def update_cube(self, new_cube_id: str) -> bool:
        """
        Updates the cube ID and imports the new cube.
        Only updates the cube_id if the import is successful.
        
        Args:
            new_cube_id: The ID of the new cube to import
            
        Returns:
            bool: True if the cube was successfully imported, False otherwise
        """
        # Store the original cube_id in case we need to revert
        original_cube_id = self.cube_id
        
        # Temporarily set the new cube_id and reset imported flag
        self.cube_id = new_cube_id
        self.cube_imported = False
        
        # Attempt to import the new cube
        success = await self.import_cube()
        
        if not success:
            # Revert to the original cube_id if import failed
            self.cube_id = original_cube_id
            self.logger.warning(f"Cube update failed, reverting to original cube: {original_cube_id}")
            
        return success
    
    async def periodic_check_status(self, bot):
        """Periodically check ready check status and update Discord message"""
        if self.ready_check_active and self.ready_check_message_id:
            await self.update_ready_check_message(bot)

    def set_bot_instance(self, bot):
        """Store the bot instance for later use"""
        self.logger.info(f"Setting bot instance for session {self.session_id}")
        self.bot = bot
        self.discord_client = bot
        
        # Immediately try to get draft channel info if needed
        if not hasattr(self, 'draft_channel_id') or not self.draft_channel_id:
            asyncio.create_task(self.fetch_draft_info())
            self.logger.info("Triggered draft info fetch to get channel ID")
        elif self.draft_channel_id:
            # Log channel verification
            try:
                channel = bot.get_channel(int(self.draft_channel_id))
                if channel:
                    self.logger.info(f"Verified channel access: #{channel.name}")
                else:
                    self.logger.warning(f"Could not find channel with ID {self.draft_channel_id} after setting bot instance")
            except Exception as e:
                self.logger.error(f"Error verifying channel access: {e}")
        
    @classmethod
    async def spawn_for_existing_session(cls, session_id, bot):
        """Create a manager for an existing session and add the bot reference"""
        # Get the draft session
        draft_session = await DraftSession.get_by_session_id(session_id)
        if not draft_session:
            return None
            
        # Check if there's already an active manager
        manager = cls.get_active_manager(session_id)
        if manager:
            manager.set_bot_instance(bot)
            
            # Also get status message ID from database if available
            if draft_session.status_message_id:
                manager.status_message_id = draft_session.status_message_id
                
            return manager
            
        # Create a new manager
        manager = cls(
            session_id=session_id,
            draft_id=draft_session.draft_id,
            cube_id=draft_session.cube
        )
        
        manager.set_bot_instance(bot)
        
        # Get status message ID if available
        if draft_session.status_message_id:
            manager.status_message_id = draft_session.status_message_id
        
        # Start connection in background
        asyncio.create_task(manager.keep_connection_alive())
        
        return manager
    
    async def update_draft_session_field(self, field_name, field_value):
        """Helper function to safely update a single field in a draft session"""
        
        try:
            async with db_session() as session:
                # Query for the object directly inside this session context
                query = select(DraftSession).filter_by(session_id=self.session_id)
                result = await session.execute(query)
                draft_session = result.scalar_one_or_none()
                
                if draft_session and hasattr(draft_session, field_name):
                    setattr(draft_session, field_name, field_value)
                    session.add(draft_session)
                    return True
                return False
        except Exception as e:
            self.logger.error(f"Error updating draft session field {field_name}: {e}")
            return False

    async def send_session_status_message(self, channel):
        """
        Sends or updates a message in the Discord channel with the current session status.
        Lists users in the session, users missing, and unexpected users.

        Args:
            channel: The Discord channel to send/update the message in.
        """
        try:
            self.logger.info(f"Sending/updating status message in channel #{channel.name}")

            # Get the draft session to compare sign-ups with session users
            draft_session = await DraftSession.get_by_session_id(self.session_id)
            if not draft_session or not draft_session.sign_ups:
                self.logger.warning("No draft session or sign-ups found for status message")
                return

            # Compute user status using helper
            user_status = self._compute_user_status(draft_session.sign_ups)

            self.logger.info(f"Status data - Present: {user_status['present']}, Missing: {user_status['missing']}, Unexpected: {user_status['unexpected']}")

            # Store status in a dictionary for easier updates
            self.session_status = {
                'present_users': sorted(list(user_status['present'])),
                'missing_users': sorted(list(user_status['missing'])),
                'unexpected_users': sorted(list(user_status['unexpected'])),
                'updated_at': datetime.now().strftime('%H:%M:%S')
            }

            # Format the message using the status dictionary
            message_content = self.format_status_message(self.session_status)

            # Try to update existing message or create a new one
            message = await self._update_or_send_message(channel, self.status_message_id, content=message_content)

            if message:
                self.last_status_update = datetime.now()
                # If we created a new message, store the ID
                if not self.status_message_id or str(message.id) != self.status_message_id:
                    self.status_message_id = str(message.id)
                    self.logger.info(f"Created new status message with ID {self.status_message_id}")
                    await self.update_draft_session_field('status_message_id', self.status_message_id)
                else:
                    self.logger.info("Successfully updated existing status message")

            return message

        except Exception as e:
            self.logger.exception(f"Error sending/updating status message: {e}")
            return None
            
    def format_status_message(self, status):
        """
        Formats the status dictionary into a readable Discord message.
        
        Args:
            status: Dictionary containing status information
            
        Returns:
            Formatted message string
        """
        message_parts = [
            f"**Draft Session Status** (Updated: {status['updated_at']})"
        ]
        
        # Present users section
        if status['present_users']:
            message_parts.append("\n**:black_joker: Drafters in Draftmancer:**")
            message_parts.append("\n".join(f"✅ {name}" for name in status['present_users']))
        
        # Missing users section
        if status['missing_users']:
            message_parts.append("\n**⏳ Waiting on:**")
            message_parts.append("\n".join(f"❌ {name}" for name in status['missing_users']))
        
        # Unexpected users section
        if status['unexpected_users']:
            message_parts.append("\n**⚠️ Unexpected Users:**")
            message_parts.append("\n".join(f"❓ {name}" for name in status['unexpected_users']))
        
        # Instructions section
        message_parts.append("\n**Instructions:**")
        
        if status['missing_users']:
            message_parts.append("• Waiting for all drafters to join Draftmancer.")
            message_parts.append("• When all drafters are present, seating order will be set automatically and a ready check will trigger.")
        else:
            message_parts.append("• All drafters are present! Setting seating order...")
            message_parts.append("• Ready check will begin shortly.")
            
        message_parts.append("• Ready check will time out after 60 seconds.")
        message_parts.append("• If everyone marks ready, the draft will begin.")
        message_parts.append("• Use `/ready` to start a new ready check if needed.")
        message_parts.append("• Use `/mutiny` to take control of the session if required.")
        
        # Join all parts with newlines
        return "\n".join(message_parts)

    async def update_status_message_after_user_change(self):
        """
        Update status message in Discord after a user joins or leaves.
        This is called from event handlers to provide real-time updates.
        """
        if not self.seating_order_set and self.draft_channel_id:
            try:
                # Get the draft session to compare sign-ups with current users
                draft_session = await DraftSession.get_by_session_id(self.session_id)
                if not draft_session or not draft_session.sign_ups:
                    self.logger.warning("No draft session or sign-ups found for status update")
                    return

                # Compute user status using helper
                user_status = self._compute_user_status(draft_session.sign_ups)

                # Update session status with fresh data
                self.session_status = {
                    'present_users': sorted(list(user_status['present'])),
                    'missing_users': sorted(list(user_status['missing'])),
                    'unexpected_users': sorted(list(user_status['unexpected'])),
                    'updated_at': datetime.now().strftime('%H:%M:%S')
                }

                # Check for unexpected users if we have expected users defined
                if self.expected_user_count > 0 and user_status['unexpected']:
                    self.logger.warning(f"Detected unexpected users: {user_status['unexpected']}")

                    # Find user IDs for unexpected users and schedule removal
                    session_users = [u for u in self.session_users if u.get('userName') != 'DraftBot']
                    for user in session_users:
                        username = user.get('userName')
                        user_id = user.get('userID')
                        if username in user_status['unexpected']:
                            self.logger.info(f"Scheduling removal of unexpected user: {username}")
                            asyncio.create_task(self.handle_unexpected_user(username, user_id))

                channel = await self._get_draft_channel()
                if channel:
                    self.logger.info(f"Updating status message after user change in channel #{channel.name}")
                    await self.send_session_status_message(channel)

                # If we now have all expected users, check seating order
                if not user_status['missing'] and self.users_count >= self.expected_user_count:
                    self.logger.info("All expected users are now present, checking seating order. checking session stage from update_status_message_after_user_change")
                    await self.check_session_stage_and_organize()
                    
            except Exception as e:
                self.logger.exception(f"Error in update_status_message_after_user_change: {e}")
        else:
            self.logger.debug("Skipping status update - seating already set or no channel ID")

    async def handle_unexpected_user(self, username, user_id):
        """
        Handles unexpected user by posting a warning message, waiting 5 seconds, then removing them.

        Args:
            username: The username of the unexpected user
            user_id: The user ID in the Draftmancer session
        """
        # Set a flag to indicate we're removing a user
        # This will prevent triggering ready checks during this process
        self.removing_unexpected_user = True

        try:
            channel = await self._get_draft_channel()
            if not channel:
                self.removing_unexpected_user = False
                return

            # Post initial warning message
            warning_message = await channel.send(f"⚠️ **Unexpected User Joined: {username}**. This user will be removed in 5 seconds.")
            self.logger.info(f"Posted warning message for unexpected user {username}")

            # Wait 5 seconds
            await asyncio.sleep(5)

            # Remove the user
            self.logger.info(f"Removing unexpected user {username} with ID {user_id}")
            await self.socket_client.emit('removePlayer', user_id)

            # Update the message
            await warning_message.edit(content=f"🚫 **Unexpected User ({username}) has been removed**.")
            self.logger.info(f"Successfully removed unexpected user {username}")

            # Update status after user change, but don't trigger ready checks yet
            await self.update_status_message_after_user_change()

            # Wait a brief moment to ensure user removal is processed
            await asyncio.sleep(1)

        except Exception as e:
            self.logger.exception(f"Error handling unexpected user {username}: {e}")
        finally:
            # Reset the flag after user removal is complete
            self.removing_unexpected_user = False

            # After removing user and resetting flag, check if we need to initiate a ready check
            if not self.ready_check_active and self.expected_user_count > 0:
                draft_session = await DraftSession.get_by_session_id(self.session_id)
                if draft_session and draft_session.sign_ups:
                    user_status = self._compute_user_status(draft_session.sign_ups)

                    # If all expected users are present and no unexpected users remain,
                    # check session stage which may trigger a ready check if appropriate
                    if not user_status['missing'] and not user_status['unexpected']:
                        self.logger.info("All users correct after removal, checking session stage")
                        await self.check_session_stage_and_organize()