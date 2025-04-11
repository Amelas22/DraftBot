import asyncio
import socketio
from loguru import logger
from functools import wraps
import random
from config import get_draftmancer_websocket_url
from datetime import datetime
from models.draft_session import DraftSession

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
    def __init__(self, session_id: str, draft_id: str, cube_id: str):
        self.session_id = session_id
        self.draft_id = draft_id
        self.cube_id = cube_id
        self.sio = socketio.AsyncClient()
        self.cube_imported = False
        self.users_count = 0  # Track number of other users
        
        # Seating order variables
        self.session_users = []
        self.seating_attempts = 0
        self.seating_order_set = False
        self.last_db_check_time = None
        self.db_check_cooldown = 15
        self.expected_user_count = None
        self.desired_seating_order = None

        # Add connection state tracking
        self._connection_lock = asyncio.Lock()
        self._is_connecting = False
        self._should_disconnect = False
        self._seating_lock = asyncio.Lock()  # Lock for seating attempts

        # Create a contextualized logger for this instance
        self.logger = logger.bind(
            draft_id=self.draft_id,
            session_id=self.session_id,
            cube_id=self.cube_id
        )
        
        # Register this instance in the global registry
        ACTIVE_MANAGERS[session_id] = self
        self.logger.info(f"Registered manager for session {session_id} in active managers registry")
        
        @self.sio.event
        async def connect():
            self.logger.info(f"Connected to websocket for draft_id: DB{self.draft_id}")
            if not self.cube_imported:
                await self.import_cube()

        @self.sio.event
        async def connect_error(data):
            self.logger.error(f"Connection failed for draft_id: DB{self.draft_id}")

        @self.sio.event
        async def disconnect():
            self.logger.info(f"Disconnected from draft_id: DB{self.draft_id}")

        # Listen for user updates
        @self.sio.on('updateUser')
        async def on_user_update(data):
            if data.get('userID') != 'DraftBot':
                self.logger.info(f"Another user joined/updated: {data}")

        # Listen for user changes in the session
        @self.sio.on('sessionUsers')
        async def on_session_users(users):
            self.logger.debug(f"Raw users data received: {users}")
            
            # Store the complete user data
            self.session_users = users
            
            # Count non-bot users
            non_bot_users = [user for user in users if user.get('userName') != 'DraftBot']
            previous_count = self.users_count
            self.users_count = len(non_bot_users)  # Only count non-bot users
            
            self.logger.info(
                f"Users update: Total users={len(users)}, Non-bot users={self.users_count}, "
                f"User IDs={[user.get('userID') for user in non_bot_users]}"
            )
            
            # IMPORTANT: If we've reached the expected count of users, check immediately
            if (self.expected_user_count is not None and 
                previous_count < self.expected_user_count and 
                self.users_count >= self.expected_user_count):
                
                self.logger.info(f"Reached expected user count! Attempting seating order")
                await self.attempt_seating_order(self.desired_seating_order)
                return
            
            # Otherwise, check on our regular schedule
            current_time = datetime.now()
            if (self.last_db_check_time is None or 
                (current_time - self.last_db_check_time).total_seconds() > self.db_check_cooldown):
                
                self.last_db_check_time = current_time
                await self.check_session_stage_and_organize()

        @self.sio.on('storedSessionSettings')
        async def on_stored_settings(data):
            self.logger.info(f"Received updated session settings: {data}")

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
                
                # Check if we have enough users to attempt setting the order
                if self.users_count >= self.expected_user_count:  # Changed to >= since we're only counting non-bot users
                    await self.attempt_seating_order(self.desired_seating_order)
                else:
                    self.logger.info(f"Not enough users yet. Waiting for {self.expected_user_count - self.users_count} more")
                    
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
            else:
                self.logger.warning(f"Failed to set seating order, missing users: {missing_users}")
                
                if self.seating_attempts >= 4:
                    self.logger.error(f"Failed to set seating order after {self.seating_attempts} attempts")
                    await self.notify_seating_failure(missing_users)

    @exponential_backoff(max_retries=10, base_delay=1)
    async def set_seating_order(self, desired_username_order):
        """
        Sets the seating order for the draft based on usernames.
        Bot is a spectator and not included in seating order.
        """
        if not self.sio.connected:
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
                
            # Set the seating order using userIDs (bot not included)
            self.logger.info(f"Setting seating order: {user_id_order}")
            await self.sio.emit('setSeating', user_id_order)
            
            # Return success status and any missing users
            if missing_users:
                if len(missing_users) < len(desired_username_order) // 2:
                    self.logger.info(f"Only missing a few users, considering it a success anyway")
                    return True, []
                return False, missing_users
            
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

    async def disconnect_safely(self):
        """
        Central method to handle disconnection safely and consistently.
        Ensures proper sequence of operations: 
        1. Set owner as player first
        2. Transfer ownership 
        3. Disconnect
        """
        if not self.sio.connected:
            return
        
        self._should_disconnect = True
        try:
            try:
                self.logger.info("Setting owner as player before transferring ownership")
                await self.sio.emit('setOwnerIsPlayer', True)
                await asyncio.sleep(1)  # Increased delay to ensure the setting is processed
            except Exception as e:
                self.logger.warning(f"Failed to set owner as player: {e}")
            
            ownership_transferred = False
            
            # Get the latest session data 
            draft_session = await DraftSession.get_by_session_id(self.session_id)
            if draft_session and draft_session.draftmancer_role_users:
                self.logger.info(f"Found Draftmancer users: {draft_session.draftmancer_role_users}")
                
                # Match draftmancer_role_users (display names) with session users
                potential_owners = []
                
                for user in self.session_users:
                    # Skip the bot itself
                    if user.get('userName') == 'DraftBot':
                        continue
                        
                    # Check if this user's display name is in draftmancer_role_users
                    if user.get('userName') in draft_session.draftmancer_role_users:
                        potential_owners.append(user)
                        self.logger.info(f"Found potential Draftmancer User: {user.get('userName')}")
                
                # If we found potential owners, select one randomly
                if potential_owners:
                    selected_owner = random.choice(potential_owners)
                    new_owner_id = selected_owner.get('userID')
                    new_owner_name = selected_owner.get('userName')
                    
                    self.logger.info(f"Transferring ownership to Draftmancer user: {new_owner_name}")
                    await self.sio.emit('setSessionOwner', new_owner_id)
                    await asyncio.sleep(1)  # Increased delay to ensure command processes
                    ownership_transferred = True
            
            # If no Draftmancer users found or transfer failed, choose any random non-bot user
            if not ownership_transferred:
                non_bot_users = [user for user in self.session_users if user.get('userName') != 'DraftBot']
                
                if non_bot_users:
                    random_user = random.choice(non_bot_users)
                    new_owner_id = random_user.get('userID')
                    new_owner_name = random_user.get('userName')
                    
                    self.logger.info(f"No Draftmancer users available. Transferring ownership to random user: {new_owner_name}")
                    await self.sio.emit('setSessionOwner', new_owner_id)
                    await asyncio.sleep(1)  # Increased delay
                else:
                    self.logger.warning("No eligible users found to transfer ownership")
            
            # Disconnect
            await self.sio.disconnect()
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
        if not self.sio.connected:
            self.logger.error("Cannot update settings - socket not connected")
            return False
            
        try:
            # Send each setting individually
            self.logger.debug("Updating draft settings...")
            await self.sio.emit('setColorBalance', False)
            await self.sio.emit('setMaxPlayers', 10)
            await self.sio.emit('setDraftLogUnlockTimer', 120)
            await self.sio.emit('setDraftLogRecipients', "delayed")
            await self.sio.emit('setPersonalLogs', True)
            await self.sio.emit('teamDraft', True)  # Added teamDraft setting
            await self.sio.emit('setPickTimer', 60)
            await self.sio.emit('setOwnerIsPlayer', False)
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error during settings update: {e}")
            self.logger.exception("Full exception details:")
            return False

    @exponential_backoff(max_retries=10, base_delay=1)
    async def import_cube(self):
        try:
            import_data = {
                "service": "Cube Cobra",
                "cubeID": self.cube_id,
                "matchVersions": True
            }
            
            # Create a Future to wait for the callback
            future = asyncio.Future()
            
            def ack(response):
                if 'error' in response:
                    self.logger.error(f"Import cube error: {response['error']}")
                    future.set_result(False)
                else:
                    self.logger.info("Cube import acknowledged")
                    self.cube_imported = True
                    future.set_result(True)

            await self.sio.emit('importCube', import_data, callback=ack)
            self.logger.info(f"Sent cube import request for {self.cube_id}")
            
            # Wait for the callback to complete
            success = await future
            return success
            
        except Exception as e:
            self.logger.error(f"Fatal error during cube import: {e}")
            if self.sio.connected:
                await self.sio.disconnect()
            return False

    async def keep_connection_alive(self):
        """
        Manages the websocket connection lifecycle, including setup, monitoring, and cleanup.
        """
        # Prevent multiple concurrent connection attempts
        async with self._connection_lock:
            if self._is_connecting:
                self.logger.warning("Connection attempt already in progress, skipping...")
                return
            self._is_connecting = True
            self._should_disconnect = False

        try:
            self.logger.info(f"Starting connection task for draft_id: DB{self.draft_id}")
            websocket_url = get_draftmancer_websocket_url(self.draft_id)
            
            # Connect to the websocket
            if self.sio.connected:
                self.logger.warning("Socket is already connected, disconnecting first...")
                await self.disconnect_safely()

            await self.sio.connect(
                websocket_url,
                transports='websocket',
                wait_timeout=10
            )
            
            # If initial cube import fails, end the task
            if not self.cube_imported and not await self.import_cube():
                self.logger.error("Initial cube import failed, ending connection task")
                return

            # Update draft settings after successful cube import
            if not await self.update_draft_settings():
                self.logger.error("Failed to update draft settings, ending connection task")
                return

            # Monitor the session until conditions are met
            while not self.seating_order_set:  # Changed condition to be based on seating
                if not self.sio.connected:
                    self.logger.error("Lost connection, ending connection task")
                    return
                
                try:
                    await self.sio.emit('getUsers')
                    
                    # If we have enough users and seating is set, we can exit
                    if self.seating_order_set:
                        self.logger.info("Seating order is set, preparing to disconnect")
                        break
                    
                    # Check if we have enough users and the stage is set
                    if (self.expected_user_count is not None and 
                        self.users_count >= self.expected_user_count):
                        
                        self.logger.info("Found enough users, checking session stage")
                        await self.check_session_stage_and_organize()
                        
                        await asyncio.sleep(5)  # Check more frequently when we have enough users
                    else:
                        await asyncio.sleep(20)  # Check less frequently while waiting for users
                        
                except Exception as e:
                    self.logger.exception(f"Error while monitoring session: {e}")
                    await asyncio.sleep(5)  # Brief delay on error before retrying
            
            # If seating order was set successfully, wait briefly before disconnecting
            if self.seating_order_set:
                self.logger.info("Seating order confirmed, waiting briefly before disconnect...")
                await self.disconnect_after_delay(2)
            else:
                self.logger.warning("Ending connection without successful seating order")
                await self.disconnect_safely()
                
        except Exception as e:
            self.logger.exception(f"Fatal error in keep_connection_alive: {e}")
        finally:
            self._is_connecting = False
            # Ensure we always disconnect cleanly
            await self.disconnect_safely()

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