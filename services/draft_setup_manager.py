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
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiobotocore.session import get_session
from config import get_draftmancer_websocket_url, get_draftmancer_base_url
from models.draft_session import DraftSession
from bot_registry import get_bot

# Constants
READY_CHECK_INSTRUCTIONS = (
    "If the seating order is wrong, or if someone missed the ready check, please run `/ready` again — this will reset the seating order and start a new ready check. "
    "You can also use `/mutiny` to take control if needed."
)

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
    def __init__(self, session_id: str, draft_id: str, cube_id: str):
        self.session_id = session_id
        self.draft_id = draft_id
        self.cube_id = cube_id
        self.sio = socketio.AsyncClient()
        self.cube_imported = False
        self.users_count = 0  # Track number of other users
    
        # Seating Order Variables
        self.session_users = []
        self.seating_attempts = 0
        self.seating_order_set = False
        self.last_db_check_time = None
        self.db_check_cooldown = 15
        self.expected_user_count = 0
        self.desired_seating_order = None

        # Add connection state tracking
        self._connection_lock = asyncio.Lock()
        self._is_connecting = False
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

        # Draft logs variables
        self.logs_collection_attempted = False
        self.logs_collection_in_progress = False
        self.logs_collection_success = False
        self.session_type = "team"  # Default to team drafts
        self.guild_id = None
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

        # Add a listener to capture draft logs
        @self.sio.on('draftLog')
        async def on_draft_log(draft_log):
            self.logger.info(f"Received draft log for session: {draft_log.get('sessionID')}")
            # Store the draft log
            self.current_draft_log = draft_log
            
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
                user_name = data.get('userName', 'Unknown')
                self.logger.info(f"Another user joined/updated: {data}")
                # Check if this is during an active ready check
                if self.ready_check_active:
                    self.logger.info(f"User {user_name} joined/left during active ready check - invalidating ready check")
                    # Just invalidate the ready check without trying to restart it
                    await self.invalidate_ready_check(user_name)
                # Update Discord status message immediately
                # This will auto-initiate a new ready check when all users are present
                await self.update_status_message_after_user_change()

        # Listen for user changes in ready state status
        @self.sio.on('setReady')
        async def on_user_ready(userID, readyState):
            await self.handle_user_ready_update(userID, readyState)
        
        # Listen for Draft Completion
        @self.sio.on('endDraft')
        async def on_draft_end(data=None):
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
                            from session import AsyncSessionLocal
                            from sqlalchemy import select

                            async with AsyncSessionLocal() as db_session:
                                stmt = select(DraftSession).filter(DraftSession.session_id == self.session_id)
                                session = await db_session.scalar(stmt)
                                if session and session.draft_chat_channel:
                                    self.logger.info(f"Rooms already existed for session {self.session_id} - skipping creation")
                                else:
                                    await channel.send("Failed to create rooms and pairings. Check logs for details.")
                else:
                    self.logger.info("Could not find guild")
                # # Schedule log collection after a delay to ensure all data is available
                # if not self.logs_collection_attempted:
                #     asyncio.create_task(self.schedule_log_collection(300))

        # Listen for Pause or Unpause (Resume)
        @self.sio.on('draftPaused')
        async def on_draft_paused(data):
            self.logger.info(f"Draft paused event received: {data}")
            self.draftPaused = True

        @self.sio.on('draftResumed')
        async def on_draft_resumed(data):
            self.logger.info(f"Draft resumed event received: {data}")
            self.draftPaused = False
            
        # Listen for user changes in the session
        @self.sio.on('sessionUsers')
        async def on_session_users(users):
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

        @self.sio.on('storedSessionSettings')
        async def on_stored_settings(data):
            self.logger.info(f"Received updated session settings: {data}")

    async def schedule_log_collection(self, delay_seconds):
        """Schedule log collection after a delay to ensure all data is available"""
        try:
            self.logger.info(f"Scheduling log collection in {delay_seconds} seconds")
            await asyncio.sleep(delay_seconds)
            await self.collect_draft_logs()
        except Exception as e:
            self.logger.exception(f"Error scheduling log collection: {e}")
    
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
            
            # If we collected logs successfully, disconnect after a short delay
            if self.logs_collection_success:
                self.logger.info("Log collection successful, scheduling disconnection in 10 seconds")
                # Short delay to allow any final processing/messages to complete
                await asyncio.sleep(10)
                self._should_disconnect = True
                await self.disconnect_safely()
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
            from database.db_session import db_session
            from sqlalchemy import select
            from models.draft_session import DraftSession

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
            if self.discord_client and self.guild_id:
                await self.send_magicprotools_embed(draft_data)
            
            return upload_successful
                
        except Exception as e:
            self.logger.exception(f"Error saving draft log data: {e}")
            return False

    async def save_to_digitalocean_spaces(self, draft_data):
        """Upload draft log data to DigitalOcean Spaces"""
        DO_SPACES_REGION = os.getenv("DO_SPACES_REGION")
        DO_SPACES_ENDPOINT = os.getenv("DO_SPACES_ENDPOINT")
        DO_SPACES_KEY = os.getenv("DO_SPACES_KEY")
        DO_SPACES_SECRET = os.getenv("DO_SPACES_SECRET")
        DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET")
        
        if not all([DO_SPACES_REGION, DO_SPACES_ENDPOINT, DO_SPACES_KEY, DO_SPACES_SECRET, DO_SPACES_BUCKET]):
            self.logger.warning("Missing DigitalOcean Spaces configuration, skipping upload")
            return False
        
        start_time = draft_data.get("time")
        draft_id = draft_data.get("sessionID")
        
        session = get_session()
        async with session.create_client(
            's3',
            region_name=DO_SPACES_REGION,
            endpoint_url=DO_SPACES_ENDPOINT,
            aws_access_key_id=DO_SPACES_KEY,
            aws_secret_access_key=DO_SPACES_SECRET
        ) as s3_client:
            try:
                folder = "swiss" if self.session_type == "swiss" else "team"
                object_name = f'{folder}/{self.cube_id}-{start_time}-{draft_id}.json'
                await s3_client.put_object(
                    Bucket=DO_SPACES_BUCKET,
                    Key=object_name,
                    Body=json.dumps(draft_data),
                    ContentType='application/json',
                    ACL='public-read'
                )
                self.logger.info(f"Draft log data uploaded to DigitalOcean Space: {object_name}")
                
                # If upload successful, also generate and upload MagicProTools format logs
                await self.process_draft_logs_for_magicprotools(draft_data, s3_client, DO_SPACES_BUCKET)
                
                return True
            except Exception as e:
                self.logger.error(f"Error uploading to DigitalOcean Space: {e}")
                return False

    async def process_draft_logs_for_magicprotools(self, draft_data, s3_client, bucket_name):
        """Process the draft log and generate formatted logs for each player."""
        try:
            session_id = draft_data.get("sessionID")
            folder = "swiss" if self.session_type == "swiss" else "team"
            
            # Process each user
            for user_id, user_data in draft_data["users"].items():
                user_name = user_data["userName"]
                
                # Convert to MagicProTools format
                mpt_format = self.convert_to_magicprotools_format(draft_data, user_id)
                
                # Create file name for this user's log
                user_filename = f"DraftLog_{user_id}.txt"
                
                # Upload to DO Spaces
                txt_key = f"draft_logs/{folder}/{session_id}/{user_filename}"
                await s3_client.put_object(
                    Bucket=bucket_name,
                    Key=txt_key,
                    Body=mpt_format,
                    ContentType='text/plain',
                    ACL='public-read'
                )
                
                self.logger.info(f"MagicProTools format log for {user_name} uploaded: {txt_key}")
                
            self.logger.info(f"All MagicProTools format logs generated and uploaded for draft {session_id}")
            return True
        except Exception as e:
            self.logger.error(f"Error generating MagicProTools format logs: {e}")
            return False

    def convert_to_magicprotools_format(self, draft_log, user_id):
        """Convert a draft log JSON to MagicProTools format for a specific user."""
        output = []
        
        # Basic draft info
        output.append(f"Event #: {draft_log['sessionID']}_{draft_log['time']}")
        output.append(f"Time: {datetime.fromtimestamp(draft_log['time']/1000).strftime('%a, %d %b %Y %H:%M:%S GMT')}")
        output.append(f"Players:")
        
        # Add player names
        for player_id, user_data in draft_log['users'].items():
            if player_id == user_id:
                output.append(f"--> {user_data['userName']}")
            else:
                output.append(f"    {user_data['userName']}")
        
        output.append("")
        
        # Determine booster header
        if (draft_log.get('setRestriction') and 
            len(draft_log['setRestriction']) == 1 and
            len([card for card in draft_log['carddata'].values() if card['set'] == draft_log['setRestriction'][0]]) >= 
            0.5 * len(draft_log['carddata'])):
            booster_header = f"------ {draft_log['setRestriction'][0].upper()} ------"
        else:
            booster_header = "------ Cube ------"
        
        # Group picks by pack
        picks = draft_log['users'][user_id]['picks']
        picks_by_pack = {}
        
        for pick in picks:
            pack_num = pick['packNum']
            if pack_num not in picks_by_pack:
                picks_by_pack[pack_num] = []
            picks_by_pack[pack_num].append(pick)
        
        # Sort packs and picks
        for pack_num in picks_by_pack:
            picks_by_pack[pack_num].sort(key=lambda x: x['pickNum'])
        
        # Process each pack
        for pack_num in sorted(picks_by_pack.keys()):
            output.append(booster_header)
            output.append("")
            
            for pick in picks_by_pack[pack_num]:
                output.append(f"Pack {pick['packNum'] + 1} pick {pick['pickNum'] + 1}:")
                
                # Get the picked card indices
                picked_indices = pick['pick']
                
                for idx, card_id in enumerate(pick['booster']):
                    # Get card name
                    card_name = draft_log['carddata'][card_id]['name']
                    
                    # Handle split/double-faced cards
                    if 'back' in draft_log['carddata'][card_id]:
                        back_name = draft_log['carddata'][card_id]['back']['name']
                        card_name = f"{card_name} // {back_name}"
                    
                    # Check if this card was picked
                    if idx in picked_indices:
                        prefix = "--> "
                    else:
                        prefix = "    "
                    
                    output.append(f"{prefix}{card_name}")
                
                output.append("")
        
        return "\n".join(output)

    async def submit_to_mpt_api(self, user_id, draft_data, api_key):
        """Submit draft data directly to MagicProTools API."""
        try:
            # Convert to MagicProTools format
            mpt_format = self.convert_to_magicprotools_format(draft_data, user_id)
            
            # Create the API request
            url = "https://magicprotools.com/api/draft/add"
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://draftmancer.com"
            }
            data = {
                "draft": mpt_format,
                "apiKey": api_key,
                "platform": "mtgadraft"
            }
            
            # Encode the data
            encoded_data = "&".join([f"{k}={urllib.parse.quote(v)}" for k, v in data.items()])
            
            # Make the request
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=encoded_data) as response:
                    if response.status == 200:
                        json_response = await response.json()
                        if "url" in json_response and not json_response.get("error"):
                            self.logger.info(f"Successfully submitted to MagicProTools API for user {user_id}")
                            return json_response["url"]
                        else:
                            self.logger.warning(f"MagicProTools API error: {json_response.get('error', 'Unknown error')}")
                    else:
                        self.logger.warning(f"MagicProTools API returned status {response.status}")
            
            return None  # Return None if unsuccessful
        except Exception as e:
            self.logger.error(f"Error submitting to MagicProTools API: {e}")
            return None

    async def send_magicprotools_embed(self, draft_data):
        """Find draft-logs channel and send the embed if found."""
        try:
            # Import locally to avoid circular imports
            import discord
            from database.db_session import db_session
            from sqlalchemy import select
            from models.draft_session import DraftSession
            
            # Find the guild
            guild = self.discord_client.get_guild(int(self.guild_id))
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
                            await check_and_post_victory_or_draw(self.discord_client, self.session_id)
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
            import discord
            from models.match import MatchResult
            from database.db_session import db_session
            from sqlalchemy import select
            from models.draft_session import DraftSession
            
            DO_SPACES_REGION = os.getenv("DO_SPACES_REGION")
            DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET")
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
                if idx < len(sign_up_display_names):
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
                
                # Generate URLs
                txt_key = f"draft_logs/{folder}/{session_id}/DraftLog_{user_id}.txt"
                txt_url = f"https://{DO_SPACES_BUCKET}.{DO_SPACES_REGION}.digitaloceanspaces.com/{txt_key}"
                mpt_url = f"https://magicprotools.com/draft/import?url={urllib.parse.quote(txt_url)}"
                
                # Direct API method
                mpt_api_key = os.getenv("MPT_API_KEY")
                final_mpt_url = mpt_url  # Default to import URL
                
                if mpt_api_key:
                    try:
                        direct_mpt_url = await self.submit_to_mpt_api(user_id, draft_data, mpt_api_key)
                        if direct_mpt_url:
                            # If API call successful, use the direct URL
                            final_mpt_url = direct_mpt_url
                            embed.add_field(
                                name=display_name,
                                value=f"[View Raw Log]({txt_url}) | [View on MagicProTools]({direct_mpt_url})",
                                inline=False
                            )
                            
                            # Get Discord ID and store the link in our dictionary
                            if discord_id:
                                magicprotools_links[discord_id] = {
                                    "name": discord_name_by_user_id.get(user_id, user_name),
                                    "link": direct_mpt_url
                                }
                            
                            continue
                    except Exception as e:
                        self.logger.error(f"Error submitting to MagicProTools API for {user_name}: {e}")
                        # Fall back to URL method
                
                # Fallback: Add field with raw log link and import link
                embed.add_field(
                    name=display_name,
                    value=f"[View Raw Log]({txt_url}) | [Import to MagicProTools]({mpt_url})",
                    inline=False
                )
                
                # Get Discord ID and store the link in our dictionary
                if discord_id:
                    magicprotools_links[discord_id] = {
                        "name": discord_name_by_user_id.get(user_id, user_name),
                        "link": final_mpt_url
                    }
            
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
            import discord
            return discord.Embed(
                title=f"Draft Log: {draft_data.get('sessionID')}",
                description="Error generating MagicProTools links. Check logs for details.",
                color=0xFF0000  # Red color
            )

    def get_pack_first_picks(self, draft_data, user_id):
        """Extract the first pick card name for each pack for a specific user."""
        pack_first_picks = {}
        try:
            # Get user's picks
            user_picks = draft_data['users'][user_id]['picks']
            
            # Find the first pick for each pack
            for pick in user_picks:
                pack_num = pick['packNum']
                pick_num = pick['pickNum']
                
                # Only consider the first pick (pick 0) for each pack
                if pick_num == 0:
                    # Get the picked card indices
                    picked_indices = pick['pick']
                    if not picked_indices:
                        pack_first_picks[str(pack_num)] = "Unknown"
                        continue
                    
                    # Get the card ID and name
                    first_picked_idx = picked_indices[0]
                    card_id = pick['booster'][first_picked_idx]
                    card_name = draft_data['carddata'][card_id]['name']
                    
                    # Handle split/double-faced cards
                    if 'back' in draft_data['carddata'][card_id]:
                        back_name = draft_data['carddata'][card_id]['back']['name']
                        card_name = f"{card_name} // {back_name}"
                    
                    pack_first_picks[str(pack_num)] = card_name
            
            return pack_first_picks
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
            # Get username sets
            session_usernames = {
                user.get('userName') for user in self.session_users 
                if user.get('userName') != 'DraftBot'
            }
            signup_usernames = set(draft_session.sign_ups.values())
            missing_users = signup_usernames - session_usernames
            
            if missing_users:
                # Users are missing, can't start ready check
                channel = bot.get_channel(int(self.draft_channel_id))
                if channel:
                    missing_users_str = ", ".join(missing_users)
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
            try:
                channel = bot.get_channel(int(self.draft_channel_id))
                await channel.send(
                    f"⚠️ **Seating order verification failed!** {seating_message}\n\n"
                    f"Use `/mutiny` to take control and fix the seating manually, "
                    f"or try `/ready` again when all players have reconnected."
                )
            except Exception as e:
                self.logger.exception(f"Error sending seating failure message: {e}")
                
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
            
            # Send initial ready check message to Discord
            channel = bot.get_channel(int(self.draft_channel_id))
            if not channel:
                channel = await bot.fetch_channel(int(self.draft_channel_id))
                if not channel:
                    self.logger.error(f"Could not find channel with ID {self.draft_channel_id}")
                    self.ready_check_active = False
                    return False
                    
            message = await channel.send(
                f"Seating order set. Draftmancer Readycheck in progress: 0/{self.expected_user_count} ready.\n"
                f"{READY_CHECK_INSTRUCTIONS}"
            )
            
            # Store message ID safely
            self.ready_check_message_id = str(message.id)
            await self.update_draft_session_field('ready_check_message_id', str(message.id))
            
            # Start timeout timer
            self.ready_check_timer = asyncio.create_task(self.ready_check_timeout(90, bot))
            
            # Emit ready check to Draftmancer
            await self.sio.emit('readyCheck')
            
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
        if not self.ready_check_message_id or not self.draft_channel_id:
            self.logger.error("Cannot update message - missing message ID or channel ID")
            return False
            
        try:
            self.logger.info(f"Updating ready check message {self.ready_check_message_id} in channel {self.draft_channel_id}")
            channel = bot.get_channel(int(self.draft_channel_id))
            if not channel:
                try:
                    self.logger.info("Channel not found in cache, attempting to fetch")
                    channel = await bot.fetch_channel(int(self.draft_channel_id))
                except Exception as e:
                    self.logger.error(f"Failed to fetch channel: {e}")
                    return False
                
            if not channel:
                self.logger.error(f"Channel with ID {self.draft_channel_id} not found")
                return False
                
            try:
                message = await channel.fetch_message(int(self.ready_check_message_id))
                if message:
                    ready_count = len(self.ready_users)
                    new_content = (
                        f"Seating order set. Draftmancer Readycheck in progress: {ready_count}/{self.expected_user_count} ready.\n"
                        f"{READY_CHECK_INSTRUCTIONS}"
                    )
                    self.logger.info(f"Updating message to show {ready_count}/{self.expected_user_count} ready")
                    await message.edit(content=new_content)
                    return True
                else:
                    self.logger.error("Message object not found after fetch")
                    return False
            except Exception as e:
                self.logger.error(f"Failed to edit message: {e}")
                return False
        except Exception as e:
            self.logger.error(f"Error updating ready check message: {e}")
            return False

    async def ready_check_timeout(self, seconds, bot):
        """Handles timeout for the ready check"""
        try:
            await asyncio.sleep(seconds)
            
            # If we reach here, the ready check timed out
            if self.ready_check_active:
                self.logger.info("Ready check timed out, but continuing to track readiness")
                self.ready_check_active = False
                
                # Initialize post-timeout tracking with currently ready users
                self.post_timeout_ready_users = self.ready_users.copy()
                
                # Identify which users weren't ready at timeout
                missing_users = []
                for user in self.session_users:
                    if (user.get('userName') != 'DraftBot' and  # Exclude bot
                        user.get('userID') not in self.ready_users):
                        missing_users.append(user.get('userName'))
                
                # Format missing users for display
                missing_text = ""
                if missing_users:
                    missing_text = f"\nWaiting for: **{', '.join(missing_users)}**"
                
                # Prepare the timeout message
                timeout_message = f"⚠️ **Ready check failed!** Timed out after {seconds} seconds.{missing_text}\n" \
                                  f"A new ready check will start automatically when all players are present."
                
                # Get the channel - needed for either approach
                try:
                    channel = bot.get_channel(int(self.draft_channel_id))
                    if not channel:
                        self.logger.error(f"Could not find channel with ID {self.draft_channel_id}")
                    else:
                        # Try to update existing message first
                        message_updated = False
                        if self.ready_check_message_id:
                            try:
                                message = await channel.fetch_message(int(self.ready_check_message_id))
                                await message.edit(content=timeout_message)
                                message_updated = True
                                self.logger.info("Ready check message updated successfully for timeout")
                            except Exception as e:
                                self.logger.error(f"Failed to update ready check message on timeout: {e}")
                                # Continue to fallback
                        
                        # Always send a new notification for timeout with instructions
                        try:
                            await channel.send(
                                f"⚠️ Ready Check Failed.{missing_text}\n"
                                f"{READY_CHECK_INSTRUCTIONS}"
                            )
                        except Exception as e:
                            self.logger.error(f"Failed to send timeout message: {e}")
                except Exception as e:
                    self.logger.error(f"Error handling ready check timeout: {e}")
                
                # Reset message ID but keep tracking readiness
                self.ready_check_message_id = None
                self.ready_users.clear()  # Clear the regular ready set
                
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
        
        try:
            # Get bot instance
            bot = get_bot()
            if bot:
                channel = bot.get_channel(int(self.draft_channel_id))
                if not channel:
                    try:
                        channel = await bot.fetch_channel(int(self.draft_channel_id))
                    except Exception as e:
                        self.logger.error(f"Error fetching channel: {e}")
                        
                if channel:
                    self.logger.info("Sending ready success message")
                    await channel.send("🎉 All drafters ready! Draft starting in 5 seconds...")
                else:
                    self.logger.error(f"Channel not found for ID {self.draft_channel_id}")
            
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
                
        # Get the bot instance
        bot = get_bot()
        if not bot:
            self.logger.error("Could not get bot instance for ready check invalidation")
            return
            
        # Prepare the failure message
        failure_message = f"⚠️ **Ready check failed!** User {user_name} joined or left during the ready check.\n" \
                         f"A new ready check will start automatically when all players are present."
                         
        # Get the channel - needed for either approach
        try:
            channel = bot.get_channel(int(self.draft_channel_id))
            if not channel:
                self.logger.error(f"Could not find channel with ID {self.draft_channel_id}")
                return
        except Exception as e:
            self.logger.error(f"Error getting channel: {e}")
            return
            
        # Try to update existing message first
        message_updated = False
        if self.ready_check_message_id:
            try:
                message = await channel.fetch_message(int(self.ready_check_message_id))
                await message.edit(content=failure_message)
                message_updated = True
                self.logger.info("Ready check message updated successfully")
            except Exception as e:
                self.logger.error(f"Failed to update ready check message: {e}")
                # Continue to fallback
        
        # Fallback: If we couldn't update the message or don't have message ID, send a new one
        if not message_updated:
            try:
                await channel.send(failure_message)
                self.logger.info("Sent new ready check failure message")
            except Exception as e:
                self.logger.error(f"Failed to send ready check failure message: {e}")
            
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
            await self.sio.emit('startDraft', callback=ack_callback)
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
                
                # Get all required info for status message
                session_usernames = {
                    user.get('userName') for user in self.session_users 
                    if user.get('userName') != 'DraftBot'
                }
                signup_usernames = set(sign_ups.values())
                missing_users = signup_usernames - session_usernames
                unexpected_users = session_usernames - signup_usernames
                present_users = session_usernames.intersection(signup_usernames)
                
                # Update our session status
                self.session_status = {
                    'present_users': sorted(list(present_users)),
                    'missing_users': sorted(list(missing_users)),
                    'unexpected_users': sorted(list(unexpected_users)),
                    'updated_at': datetime.now().strftime('%H:%M:%S')
                }
                
                # DEBUG LOGGING for Discord channel info
                self.logger.info(f"Discord channel info - draft_channel_id: {self.draft_channel_id}")
                self.logger.info(f"Discord client available: {hasattr(self, 'discord_client') and self.discord_client is not None}")
                
                # Make sure draft_channel_id is properly set - fetch again if needed
                if not self.draft_channel_id and draft_session.draft_channel_id:
                    self.draft_channel_id = draft_session.draft_channel_id
                    self.logger.info(f"Updated draft_channel_id from database: {self.draft_channel_id}")
                
                # Send/update status message to Discord channel if available
                if self.draft_channel_id and hasattr(self, "discord_client") and self.discord_client:
                    bot = self.discord_client
                    try:
                        self.logger.info(f"Attempting to get channel with ID: {self.draft_channel_id}")
                        channel = bot.get_channel(int(self.draft_channel_id))
                        
                        if channel:
                            self.logger.info(f"Found channel: #{channel.name}")
                            await self.send_session_status_message(channel)
                        else:
                            self.logger.warning(f"Channel not found with ID {self.draft_channel_id}, trying to fetch...")
                            try:
                                # Try to fetch the channel if it's not in the cache
                                channel = await bot.fetch_channel(int(self.draft_channel_id))
                                if channel:
                                    self.logger.info(f"Successfully fetched channel: #{channel.name}")
                                    await self.send_session_status_message(channel)
                                else:
                                    self.logger.error(f"Could not fetch channel with ID {self.draft_channel_id}")
                            except Exception as e:
                                self.logger.error(f"Error fetching channel: {e}")
                    except Exception as e:
                        self.logger.exception(f"Error sending status message: {e}")
                else:
                    # Log why we couldn't send a message
                    if not self.draft_channel_id:
                        self.logger.warning("No draft_channel_id available")
                    if not hasattr(self, "discord_client") or not self.discord_client:
                        self.logger.warning("No discord_client available")
                
                # Check if we have all expected users to attempt setting the order
                if not missing_users and self.users_count >= self.expected_user_count:
                    self.logger.info("All expected users are present, attempting to set seating order")
                    await self.attempt_seating_order(self.desired_seating_order)
                else:
                    self.logger.info(f"Not all users present. Missing: {missing_users}")
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
                
                if self.draft_channel_id and hasattr(self, "discord_client") and self.discord_client:
                    bot = self.discord_client
                    try:
                        channel = bot.get_channel(int(self.draft_channel_id))
                        if channel and self.status_message_id:
                            try:
                                # Update the existing status message
                                message = await channel.fetch_message(int(self.status_message_id))
                                new_content = self.format_status_message(self.session_status)
                                new_content += "\n\n✅ **Seating order set successfully! Starting ready check...**"
                                await message.edit(content=new_content)
                            except Exception as e:
                                self.logger.error(f"Error updating status message: {e}")
                    except Exception as e:
                        self.logger.error(f"Error updating status message: {e}")
                
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
                
                # If we have a Discord channel, update the status message
                if self.draft_channel_id and hasattr(self, "discord_client") and self.discord_client:
                    bot = self.discord_client
                    try:
                        channel = bot.get_channel(int(self.draft_channel_id))
                        if channel and self.status_message_id:
                            try:
                                # Update the existing status message
                                message = await channel.fetch_message(int(self.status_message_id))
                                new_content = self.format_status_message(self.session_status)
                                missing_users_str = ", ".join(missing_users)
                                new_content += f"\n\n❌ **Failed to set seating order. Missing users: {missing_users_str}**\n"
                                new_content += f"These players need to join the Draftmancer session."
                                await message.edit(content=new_content)
                            except Exception as e:
                                self.logger.error(f"Error updating status message: {e}")
                                
                            # Also send a separate notification for visibility
                            missing_users_str = ", ".join(missing_users)
                            await channel.send(
                                f"⚠️ **Seating order could not be set**\n"
                                f"Missing users: {missing_users_str}\n"
                                f"These players need to join the Draftmancer session."
                            )
                    except Exception as e:
                        self.logger.error(f"Error sending missing users message: {e}")
                
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
                
            # Only proceed if all users are present
            if missing_users:
                self.logger.warning(f"Cannot set seating order - missing users: {missing_users}")
                return False, missing_users
                
            # Set the seating order using userIDs (bot not included)
            self.logger.info(f"Setting seating order: {user_id_order}")
            await self.sio.emit('setSeating', user_id_order)
            
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
        if not self.sio.connected:
            self.logger.error("Cannot update settings - socket not connected")
            return False
            
        try:
            # Send each setting individually
            self.logger.debug("Updating draft settings...")
            await self.sio.emit('setColorBalance', False)
            await self.sio.emit('setMaxPlayers', 10)
            await self.sio.emit('setDraftLogUnlockTimer', 180)
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
        """Updated method to keep the bot connected after seating order is set"""
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
            draft_ended_time = None
            last_log_attempt_time = None
            
            while True:
                if not self.sio.connected:
                    self.logger.error("Lost connection, ending connection task")
                    return
                
                if self._should_disconnect:
                    self.logger.info("Disconnect requested, ending connection task")
                    break
                    
                try:
                    await self.sio.emit('getUsers')
                    
                    # Check if we need to set seating
                    if not self.seating_order_set and self.expected_user_count is not None:
                        try:
                            await self.sio.emit('getUsers')
                        except Exception as e:
                            self.logger.error(f"Error getting users: {e}")
                            
                        # If we already have enough users, check session stage
                        # This is a fallback in case any event updates were missed
                        if self.users_count >= self.expected_user_count:
                            current_time = datetime.now()
                            if (self.last_db_check_time is None or 
                                (current_time - self.last_db_check_time).total_seconds() > self.db_check_cooldown):
                                
                                self.last_db_check_time = current_time
                                self.logger.info("check session stage from keep connection alive for user count >= expected user count")
                                await self.check_session_stage_and_organize()
                                    
                        # Try to emit getUsers regularly for accurate counts
                        try:
                            await self.sio.emit('getUsers')
                        except Exception as e:
                            self.logger.error(f"Error getting users: {e}")
                    
                    # Draft completed logic
                    if not self.drafting and not self.draft_cancelled:
                        # If this is the first time we've noticed draft is not active
                        if draft_ended_time is None:
                            draft_ended_time = datetime.now()
                            self.logger.info(f"Draft not active, setting draft_ended_time to {draft_ended_time}")
                            
                            # # Immediately attempt to collect logs if not attempted yet
                            # if not self.logs_collection_attempted and not self.logs_collection_in_progress:
                            #     self.logger.info("Attempting to collect logs immediately")
                            #     last_log_attempt_time = datetime.now()
                            #     await self.collect_draft_logs()
                        else:
                            # If logs were collected successfully, we can disconnect
                            if self.logs_collection_success:
                                self.logger.info("Logs collected successfully, disconnecting")
                                self._should_disconnect = True
                                break
                                
                            # # If logs were attempted but failed, retry periodically (every 30 minutes)
                            # if (self.logs_collection_attempted and not self.logs_collection_success and 
                            #     last_log_attempt_time and 
                            #     (datetime.now() - last_log_attempt_time).total_seconds() > 1800):
                                
                            #     self.logger.info(f"Retrying log collection after {(datetime.now() - last_log_attempt_time).total_seconds()} seconds")
                            #     self.logs_collection_attempted = False  # Reset to allow retry
                            #     last_log_attempt_time = datetime.now()
                            #     await self.collect_draft_logs()
                    else:
                        # If draft is active again, reset the ended time
                        draft_ended_time = None
                        
                    await asyncio.sleep(10)  # Regular check interval
                        
                except Exception as e:
                    self.logger.exception(f"Error while monitoring session: {e}")
                    await asyncio.sleep(5)
                    
        except Exception as e:
            self.logger.exception(f"Fatal error in keep_connection_alive: {e}")
        finally:
            self._is_connecting = False
            # Only disconnect if requested
            if self._should_disconnect:
                await self.disconnect_safely()
                
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
                await self.sio.emit('getCurrentDraftLog', callback=on_draft_log_response)
                
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
                await self.sio.emit('shareDraftLog', draft_log)
                
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
        from database.db_session import db_session
        from sqlalchemy import select
        
        try:
            async with db_session() as session:
                # Query for the object directly inside this session context
                from models.draft_session import DraftSession
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
            
            # Get usernames from the session (excluding DraftBot)
            session_usernames = {
                user.get('userName') for user in self.session_users 
                if user.get('userName') != 'DraftBot'
            }
            
            # Get usernames from sign-ups
            signup_usernames = set(draft_session.sign_ups.values())
            
            # Calculate missing and unexpected users
            missing_users = signup_usernames - session_usernames
            unexpected_users = session_usernames - signup_usernames
            present_users = session_usernames.intersection(signup_usernames)
            
            self.logger.info(f"Status data - Present: {present_users}, Missing: {missing_users}, Unexpected: {unexpected_users}")
            
            # Store status in a dictionary for easier updates
            self.session_status = {
                'present_users': sorted(list(present_users)),
                'missing_users': sorted(list(missing_users)),
                'unexpected_users': sorted(list(unexpected_users)),
                'updated_at': datetime.now().strftime('%H:%M:%S')
            }
            
            # Format the message using the status dictionary
            message_content = self.format_status_message(self.session_status)
            
            # Try to update existing message or create a new one
            if hasattr(self, 'status_message_id') and self.status_message_id:
                try:
                    self.logger.info(f"Attempting to update existing message with ID {self.status_message_id}")
                    # Try to get the existing message
                    message = await channel.fetch_message(int(self.status_message_id))
                    # Update the existing message
                    await message.edit(content=message_content)
                    self.logger.info("Successfully updated existing status message")
                    self.last_status_update = datetime.now()
                    return message
                except Exception as e:
                    self.logger.warning(f"Could not update existing status message: {e}")
                    # Message might be deleted or too old, create a new one
            
            # Create a new status message
            self.logger.info("Creating new status message")
            new_message = await channel.send(message_content)
            self.status_message_id = str(new_message.id)
            self.last_status_update = datetime.now()
            self.logger.info(f"Created new status message with ID {self.status_message_id}")
            
            # Store in the database for persistence
            await self.update_draft_session_field('status_message_id', self.status_message_id)
            return new_message
            
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
                    
                # Get username sets 
                session_usernames = {
                    user.get('userName') for user in self.session_users 
                    if user.get('userName') != 'DraftBot'
                }
                signup_usernames = set(draft_session.sign_ups.values())
                missing_users = signup_usernames - session_usernames
                unexpected_users = session_usernames - signup_usernames
                present_users = session_usernames.intersection(signup_usernames)
                
                # Update session status with fresh data
                self.session_status = {
                    'present_users': sorted(list(present_users)),
                    'missing_users': sorted(list(missing_users)),
                    'unexpected_users': sorted(list(unexpected_users)),
                    'updated_at': datetime.now().strftime('%H:%M:%S')
                }
                
                # Check if we have a Discord client and channel
                if hasattr(self, "discord_client") and self.discord_client:
                    bot = self.discord_client
                    try:
                        channel = bot.get_channel(int(self.draft_channel_id))
                        if channel:
                            self.logger.info(f"Updating status message after user change in channel #{channel.name}")
                            await self.send_session_status_message(channel)
                        else:
                            # Try to fetch the channel if it's not in cache
                            try:
                                channel = await bot.fetch_channel(int(self.draft_channel_id))
                                if channel:
                                    await self.send_session_status_message(channel)
                                else:
                                    self.logger.warning(f"Could not fetch channel with ID {self.draft_channel_id}")
                            except Exception as e:
                                self.logger.error(f"Error fetching channel: {e}")
                    except Exception as e:
                        self.logger.error(f"Error updating status message after user change: {e}")
                else:
                    self.logger.debug("Cannot update status message - Discord client not available")
                    
                # If we now have all expected users, check seating order
                if not missing_users and self.users_count >= self.expected_user_count:
                    self.logger.info("All expected users are now present, checking seating order. checking session stage from update_status_message_after_user_change")
                    await self.check_session_stage_and_organize()
                    
            except Exception as e:
                self.logger.exception(f"Error in update_status_message_after_user_change: {e}")
        else:
            self.logger.debug("Skipping status update - seating already set or no channel ID")