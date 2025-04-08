import asyncio
import socketio
from loguru import logger
from functools import wraps
import random
from config import get_draftmancer_websocket_url

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
        
        # Create a contextualized logger for this instance
        self.logger = logger.bind(
            draft_id=self.draft_id,
            session_id=self.session_id,
            cube_id=self.cube_id
        )
        
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
            
            self.users_count = len(users)
            
            self.logger.info(
                f"Users update: Total users={len(users)}, "
                f"Other users={self.users_count}, "
                f"User IDs={[user.get('userID') for user in users]}"
            )

        @self.sio.on('storedSessionSettings')
        async def on_stored_settings(data):
            self.logger.info(f"Received updated session settings: {data}")

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
        self.logger.info(f"Starting connection task for draft_id: DB{self.draft_id}")
        try:
            websocket_url = get_draftmancer_websocket_url(self.draft_id)
            
            # Connect to the websocket
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

            # Wait for at least 2 other users
            while self.users_count < 3:
                if not self.sio.connected:
                    self.logger.error("Lost connection, ending connection task")
                    return
                
                # Request current users in the session
                try:
                    await self.sio.emit('getUsers')
                #    self.logger.debug("Requested current users in session")
                except Exception as e:
                    self.logger.exception(f"Failed to request users: {e}")
                
                # self.logger.info(f"Waiting for more users... Currently {self.users_count} other users present")
                await asyncio.sleep(20)
            
            self.logger.success(f"At least 2 other users have joined the session ({self.users_count} total). Closing connection...")
                    
        except Exception as e:
            self.logger.exception(f"Fatal error in keep_connection_alive: {e}")
        finally:
            # Always try to disconnect cleanly
            try:
                if self.sio.connected:
                    await self.sio.disconnect()
                    self.logger.info("Disconnected successfully")
            except Exception as e:
                self.logger.exception(f"Error during final disconnect: {e}")