import asyncio
import socketio
import random
from loguru import logger

class DraftSocketClient:
    """
    A wrapper around socketio.AsyncClient that handles connection reliability,
    exponential backoff, and safe disconnection.
    """
    def __init__(self, resource_id: str):
        self.resource_id = resource_id  # E.g., session_id or draft_id (for logging)
        self.sio = socketio.AsyncClient()
        self._connection_lock = asyncio.Lock()
        
    @property
    def connected(self):
        return self.sio.connected

    async def connect_with_retry(self, url: str, max_retries: int = 5, base_delay: int = 2):
        """
        Attempts to connect to the given URL with exponential backoff.
        """
        retries = 0
        while retries < max_retries:
            # Check if we are already connected before trying
            if self.sio.connected:
                return True

            async with self._connection_lock:
                # Double check inside lock
                if self.sio.connected:
                    return True
                
                try:
                    logger.info(f"Connecting to {url} for {self.resource_id} (Attempt {retries + 1}/{max_retries})")
                    await self.sio.connect(url, transports='websocket', wait_timeout=10)
                    logger.info(f"Successfully connected to {url} for {self.resource_id}")
                    return True
                except socketio.exceptions.ConnectionError as e:
                    logger.warning(f"Connection error for {self.resource_id} (Attempt {retries + 1}): {e}")
                except Exception as e:
                    logger.error(f"Unexpected error connecting {self.resource_id} (Attempt {retries + 1}): {e}")
            
            retries += 1
            if retries < max_retries:
                delay = (base_delay * 2 ** (retries - 1)) + (random.uniform(0, 1))
                logger.info(f"Backing off for {delay:.2f} seconds before retry for {self.resource_id}")
                await asyncio.sleep(delay)
                
        return False

    async def disconnect(self):
        """
        Safely disconnects the socket if connected.
        """
        if self.sio.connected:
            try:
                logger.info(f"Disconnecting socket for {self.resource_id}")
                await self.sio.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting {self.resource_id}: {e}")

    async def emit(self, event, data=None, callback=None):
        """
        Wrapper for emit that checks connection status first.

        Args:
            event: The event name to emit
            data: Optional data to send with the event
            callback: Optional callback function for acknowledgment
        """
        if not self.sio.connected:
            logger.warning(f"Attempted to emit '{event}' while disconnected for {self.resource_id}")
            return False

        try:
            await self.sio.emit(event, data, callback=callback)
            return True
        except Exception as e:
            logger.error(f"Error emitting '{event}' for {self.resource_id}: {e}")
            return False
