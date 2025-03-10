import asyncio
import socketio
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

class DraftSetupManager:
    def __init__(self, session_id: str, draft_id: str, cube_id: str):
        self.session_id = session_id
        self.draft_id = draft_id
        self.cube_id = cube_id
        self.sio = socketio.AsyncClient()
        self.cube_imported = False
        self.other_users_present = False
        
        @self.sio.event
        async def connect():
            logger.info(f"Connected to websocket for draft_id: DB{self.draft_id}")
            if not self.cube_imported:
                await self.import_cube()

        @self.sio.event
        async def connect_error(data):
            logger.error(f"Connection failed for draft_id: DB{self.draft_id}")

        @self.sio.event
        async def disconnect():
            logger.info(f"Disconnected from draft_id: DB{self.draft_id}")

        # Listen for user updates
        @self.sio.on('updateUser')
        def on_user_update(data):
            if data.get('userID') != f'DraftBot':
                logger.info(f"Another user joined/updated: {data}")
                self.other_users_present = True

        # Listen for user changes in the session
        @self.sio.on('users')
        def on_users(users):
            logger.info(f"Users in session: {users}")
            # Check if there are users other than our bot
            if len(users) > 1:  # More than just our bot
                self.other_users_present = True

    async def import_cube(self):
        try:
            import_data = {
                "service": "Cube Cobra",
                "cubeID": self.cube_id,
                "matchVersions": True
            }
            
            def ack(response):
                if 'error' in response:
                    logger.error(f"Import cube error: {response['error']}")
                else:
                    logger.info("Cube import acknowledged")
                    self.cube_imported = True

            await self.sio.emit('importCube', import_data, callback=ack)
            logger.info(f"Sent cube import request for {self.cube_id}")
            
        except Exception as e:
            logger.error(f"Fatal error during cube import: {e}")
            # If import fails, we'll disconnect and let the task end
            if self.sio.connected:
                await self.sio.disconnect()
            return False
        
        return True

    async def keep_connection_alive(self):
        try:
            # Connect to the websocket
            await self.sio.connect(
                f'wss://draftmancer.com?userID=DraftBot&sessionID=DB{self.draft_id}&userName=DraftBot',
                transports='websocket',
                wait_timeout=10
            )
            
            # If initial cube import fails, end the task
            if not self.cube_imported and not await self.import_cube():
                logger.error("Initial cube import failed, ending connection task")
                return

            ping_task = None
            while not self.other_users_present:
                try:
                    if not self.sio.connected:
                        logger.error("Lost connection, ending connection task")
                        return

                    if ping_task and not ping_task.done():
                        ping_task.cancel()
                    
                    ping_task = asyncio.create_task(self.sio.emit('ping'))
                    logger.info("Ping sent to keep connection alive. Waiting for other users...")
                    await asyncio.sleep(20)
                except Exception as e:
                    logger.error(f"Error during connection maintenance: {e}")
                    return  # Any error means we should stop the task
            
            logger.info("Other users have joined the session. Closing connection...")
                    
        except Exception as e:
            logger.error(f"Fatal error in keep_connection_alive: {e}")
        finally:
            # Always try to disconnect cleanly
            try:
                if self.sio.connected:
                    await self.sio.disconnect()
            except Exception as e:
                logger.error(f"Error during final disconnect: {e}")