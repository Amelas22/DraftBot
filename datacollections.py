import socketio
import asyncio
import aiohttp
import aiobotocore
import json
import os
import pytz
import urllib.parse
from datetime import datetime, timedelta
from dotenv import load_dotenv
from sqlalchemy.future import select
from aiobotocore.session import get_session
from session import AsyncSessionLocal, DraftSession
from loguru import logger

load_dotenv()

class DraftLogManager:
    def __init__(self, session_id, draft_link, draft_id, session_type, cube, discord_client=None, guild_id=None):
        self.session_id = session_id
        self.draft_link = draft_link
        self.draft_id = draft_id
        self.session_type = session_type
        self.cube = cube
        self.first_delay = False
        self.fetch_attempts = 0
        self.connection_attempts = 0
        self.first_connection = True
        self.sio = socketio.AsyncClient()
        self.discord_client = discord_client
        self.guild_id = guild_id
        
        @self.sio.event
        async def connect():
            logger.info(f"Successfully connected to the websocket for draft_id: DB{self.draft_id}")

        @self.sio.event
        async def connect_error(data):
            logger.warning(f"Connection to the websocket failed for draft_id: DB{self.draft_id}")

        @self.sio.event
        async def disconnect():
            logger.info(f"Disconnected from the websocket for draft_id: DB{self.draft_id}")

    async def keep_draft_session_alive(self):
        keep_running = True
        if self.first_connection:
            await asyncio.sleep(900)
            self.first_connection = False
        while keep_running:
            try:
                await self.sio.connect(
                    f'wss://draftmancer.com?userID=DraftBot&sessionID=DB{self.draft_id}&userName=DraftBot',
                    transports='websocket',
                    wait_timeout=10)
                
                while True:
                    if self.fetch_attempts >= 20 or self.connection_attempts >= 20:
                        logger.warning(f"Exceeded maximum attempts for {self.draft_id}, stopping attempts and disconnecting.")
                        keep_running = False
                        break

                    data_fetched = await self.fetch_draft_log_data()
                    if data_fetched:
                        logger.info(f"Draft log data fetched and saved for {self.draft_id}, disconnecting")
                        await self.sio.disconnect()
                        return
                    else:
                        logger.info(f"{self.draft_id} log data not available attempt {self.fetch_attempts}, retrying in 5 minutes...")
                        await asyncio.sleep(300)  # Retry every 5 minutes

                    try:
                        await self.sio.emit('ping')  # Send a ping to keep the connection alive
                        await asyncio.sleep(120)  # Send a ping every 2 minutes
                    except socketio.exceptions.ConnectionError:
                        logger.warning(f"Connection to {self.draft_link} closed, retrying...")
                        self.connection_attempts += 1
                        break

            except Exception as e:
                logger.error(f"Error connecting to {self.draft_link}: {e}")
                self.connection_attempts += 1
            
            if keep_running:
                await asyncio.sleep(120)

    async def fetch_draft_log_data(self):
        url = f"https://draftmancer.com/getDraftLog/DB{self.draft_id}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as response:
                    if response.status == 200:
                        draft_data = await response.json()
                        first_user_picks = next(iter(draft_data["users"].values()))["picks"]
                        eastern = pytz.timezone('US/Eastern')
                        if not self.first_delay and not first_user_picks:
                            
                            next_fetch_time = datetime.now(pytz.utc) + timedelta(seconds=5400)
                            next_fetch_time_eastern = next_fetch_time.astimezone(eastern)
                            logger.info(f"Draft log data for {self.draft_id} has no picks, retrying at {next_fetch_time_eastern.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                            await asyncio.sleep(5400)  # Wait for ninety minutes
                            self.first_delay = True
                            self.fetch_attempts += 1
                            return await self.fetch_draft_log_data()  # Retry fetching the data
                        elif self.first_delay and not first_user_picks:
                            logger.info(f"Draft log data for {self.draft_id} has no picks, retrying in 5 minutes")
                            await asyncio.sleep(300)  # Wait for 5 minutes
                            self.fetch_attempts += 1
                            return await self.fetch_draft_log_data()
                        elif first_user_picks:
                            await self.save_draft_log_data(draft_data)
                            return True
                    else:
                        logger.warning(f"Failed to fetch draft log data: status code {response.status}")
                        self.fetch_attempts += 1
                        return False
            except Exception as e:
                logger.error(f"Exception while fetching draft log data: {e}")
                self.fetch_attempts += 1
                return False

    async def save_draft_log_data(self, draft_data):    
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                upload_successful = await self.save_to_digitalocean_spaces(draft_data)
                stmt = select(DraftSession).filter(DraftSession.session_id == self.session_id)
                draft_session = await db_session.scalar(stmt)
                
                if upload_successful and draft_session:
                    draft_session.data_received = True
                    
                    # Extract and store first picks for each user and pack
                    draftmancer_user_picks = {}
                    for user_id, user_data in draft_data["users"].items():
                        user_pack_picks = self.get_pack_first_picks(draft_data, user_id)
                        draftmancer_user_picks[user_id] = user_pack_picks
                    
                    # We need to convert Draftmancer user IDs to Discord user IDs
                    discord_user_pack_picks = {}

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
                    
                    # Store the first picks in the database with Discord IDs as keys
                    draft_session.pack_first_picks = discord_user_pack_picks
                    logger.info(f"Stored first picks for {len(discord_user_pack_picks)} users with Discord IDs as keys")
                    
                    if draft_session.victory_message_id_draft_chat and self.discord_client and self.guild_id:
                        logger.info(f"Draft victory message detected. Sending logs for {self.draft_id}")
                        await self.send_magicprotools_embed(draft_data)
                    elif draft_session.teams_start_time and self.discord_client and self.guild_id:
                        unlock_time = draft_session.teams_start_time + timedelta(seconds=9000)
                        current_time = datetime.now()
                        if current_time >= unlock_time:
                            # If 2:15 hours have already passed, post links immediately
                            logger.info(f"Draft {self.draft_id} logs are available (2+ hours since start)")
                            await self.send_magicprotools_embed(draft_data)
                        else:
                            # Schedule a task to post links after the time difference
                            time_to_wait = (unlock_time - current_time).total_seconds()
                            minutes_to_wait = time_to_wait / 60
                            logger.info(f"Draft {self.draft_id} logs will be available in {minutes_to_wait:.1f} minutes")
                            
                            # Schedule the task
                            self.discord_client.loop.create_task(self.post_links_after_delay(draft_data, time_to_wait))
                    else:
                        logger.info(f"Draft {self.draft_id} log data saved but can't determine when to post links")
                
                elif draft_session:
                    draft_session.draft_data = draft_data
                    logger.info(f"Draft log data saved in database for {self.draft_id}; SessionID: {self.session_id}")
                else:
                    logger.warning(f"Draft session {self.session_id} not found in the database")
            
            await db_session.commit()

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
            logger.error(f"Error getting first picks: {e}")
            return {}
        
    async def post_links_after_delay(self, draft_data, delay_seconds):
        """Post MagicProTools links after a specified delay."""
        try:
            logger.info(f"Scheduled posting of links for draft {self.draft_id} in {delay_seconds/60:.1f} minutes")
            await asyncio.sleep(delay_seconds)
            logger.info(f"Time's up! Posting links for draft {self.draft_id}")
            await self.send_magicprotools_embed(draft_data)
        except Exception as e:
            logger.error(f"Error posting links after delay for draft {self.draft_id}: {e}")
            
    async def send_magicprotools_embed(self, draft_data):
        """Find draft-logs channel and send the embed if found."""
        try:
            # Find the guild
            guild = self.discord_client.get_guild(self.guild_id)
            if not guild:
                logger.warning(f"Could not find guild with ID {self.guild_id}")
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
                logger.info(f"Sent MagicProTools links to #{draft_logs_channel.name} in {guild.name}")
                
                # Save the channel and message IDs to the database
                async with AsyncSessionLocal() as db_session:
                    async with db_session.begin():
                        stmt = select(DraftSession).filter_by(session_id=self.session_id)
                        result = await db_session.execute(stmt)
                        draft_session = result.scalar_one_or_none()
                        
                        if draft_session:
                            draft_session.logs_channel_id = str(draft_logs_channel.id)
                            draft_session.logs_message_id = str(message.id)
                            await db_session.commit()
                            logger.info(f"Saved logs channel and message IDs for session {self.session_id}")

                            # Update victory messages to include the logs link
                            # Import the function here to avoid circular imports
                            from utils import check_and_post_victory_or_draw
                            logger.info(f"Updating victory messages for session {self.session_id} to include logs link")
                            try:
                                await check_and_post_victory_or_draw(self.discord_client, self.session_id)
                                logger.info(f"Successfully updated victory messages with logs link for session {self.session_id}")
                            except Exception as e:
                                logger.error(f"Error updating victory messages with logs link: {e}")
                        else:
                            logger.warning(f"Draft session {self.session_id} not found, couldn't save logs message info")
                    try:                        
                        logger.info(f"Calling update_victory_messages_with_logs for session {self.session_id}")
                        await update_victory_messages_with_logs(
                            self.discord_client, 
                            self.session_id,
                            draft_session.logs_channel_id,
                            draft_session.logs_message_id
                        )
                    except Exception as e:
                        logger.error(f"Error updating victory messages with logs link: {e}")
            else:
                logger.warning(f"No 'draft-logs' channel found in guild {guild.name}, skipping embed message")
        except Exception as e:
            logger.error(f"Error sending MagicProTools embed: {e}")
            
    async def save_to_digitalocean_spaces(self, draft_data):
        DO_SPACES_REGION = os.getenv("DO_SPACES_REGION")
        DO_SPACES_ENDPOINT = os.getenv("DO_SPACES_ENDPOINT")
        DO_SPACES_KEY = os.getenv("DO_SPACES_KEY")
        DO_SPACES_SECRET = os.getenv("DO_SPACES_SECRET")
        DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET")
        
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
                object_name = f'{folder}/{self.cube}-{start_time}-{draft_id}.json'
                await s3_client.put_object(
                    Bucket=DO_SPACES_BUCKET,
                    Key=object_name,
                    Body=json.dumps(draft_data),
                    ContentType='application/json',
                    ACL='public-read'
                )
                logger.info(f"Draft log data uploaded to DigitalOcean Space: {object_name}")
                
                # If upload successful, also generate and upload MagicProTools format logs
                await self.process_draft_logs_for_magicprotools(draft_data, s3_client, DO_SPACES_BUCKET)
                
                return True
            except Exception as e:
                logger.error(f"Error uploading to DigitalOcean Space: {e}")
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
                
                logger.info(f"MagicProTools format log for {user_name} uploaded: {txt_key}")
                
            logger.info(f"All MagicProTools format logs generated and uploaded for draft {session_id}")
            return True
        except Exception as e:
            logger.error(f"Error generating MagicProTools format logs: {e}")
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

    async def generate_magicprotools_embed(self, draft_data):
        """Generate a Discord embed with MagicProTools links for all drafters and store them in the database."""
        try:
            import discord  # Import locally to avoid issues if Discord isn't available
            from models.match import MatchResult  # Import MatchResult class
            
            DO_SPACES_REGION = os.getenv("DO_SPACES_REGION")
            DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET")
            session_id = draft_data.get("sessionID")
            folder = "swiss" if self.session_type == "swiss" else "team"
            
            # Get the draft session to access sign_ups and start time
            async with AsyncSessionLocal() as db_session:
                draft_session_stmt = select(DraftSession).filter_by(session_id=self.session_id)
                result = await db_session.execute(draft_session_stmt)
                draft_session = result.scalar_one_or_none()
                
                if not draft_session:
                    logger.warning(f"Draft session not found for session ID: {self.session_id}")
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
                        match_results_result = await db_session.execute(match_results_stmt)
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
                title=f"Draft Log: Cube: {self.cube}, Session:{session_id}",
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
                    display_name = f"{team_emoji} {user_name} - {discord_name}: {record_str}{trophy_emoji}"
                
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
                        logger.error(f"Error submitting to MagicProTools API for {user_name}: {e}")
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
                    db_session.add(draft_session)
                    await db_session.commit()
                    logger.info(f"Updated DraftSession with MagicProTools links for {len(magicprotools_links)} users")
                except Exception as e:
                    logger.error(f"Error saving MagicProTools links to database: {e}")
            
            return embed
        except Exception as e:
            logger.error(f"Error generating Discord embed: {e}")
            # Return a basic embed if there's an error
            return discord.Embed(
                title=f"Draft Log: {draft_data.get('sessionID')}",
                description="Error generating MagicProTools links. Check logs for details.",
                color=0xFF0000  # Red color
            )
        
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
                            logger.info(f"Successfully submitted to MagicProTools API for user {user_id}")
                            return json_response["url"]
                        else:
                            logger.warning(f"MagicProTools API error: {json_response.get('error', 'Unknown error')}")
                    else:
                        logger.warning(f"MagicProTools API returned status {response.status}")
            
            return None  # Return None if unsuccessful
        except Exception as e:
            logger.error(f"Error submitting to MagicProTools API: {e}")
            return None

async def update_victory_messages_with_logs(discord_client, session_id, logs_channel_id, logs_message_id):
    """
    Simpler function specifically for updating victory messages with logs links.
    This bypasses the complexity of check_and_post_victory_or_draw.
    """
    logger.info(f"Starting direct update of victory messages for session {session_id}")
    import discord

    async with AsyncSessionLocal() as db_session:
        async with db_session.begin():
            # Get the draft session
            stmt = select(DraftSession).filter_by(session_id=session_id)
            result = await db_session.execute(stmt)
            draft_session = result.scalar_one_or_none()
            
            if not draft_session:
                logger.error(f"Draft session not found: {session_id}")
                return False
            
            # Check if we have victory messages to update
            if not draft_session.victory_message_id_draft_chat and not draft_session.victory_message_id_results_channel:
                logger.info(f"No victory messages to update for session {session_id}")
                return False
            
            # Get the guild
            guild = discord_client.get_guild(int(draft_session.guild_id))
            if not guild:
                logger.error(f"Guild not found: {draft_session.guild_id}")
                return False
            
            # Create the logs link
            logs_link = f"https://discord.com/channels/{draft_session.guild_id}/{logs_channel_id}/{logs_message_id}"
            logger.info(f"Created logs link: {logs_link}")
            
            # Update messages in both channels
            success = False
            
            # Update draft chat message
            if draft_session.victory_message_id_draft_chat:
                try:
                    draft_chat_channel = guild.get_channel(int(draft_session.draft_chat_channel))
                    if draft_chat_channel:
                        try:
                            message = await draft_chat_channel.fetch_message(int(draft_session.victory_message_id_draft_chat))
                            embed = message.embeds[0] if message.embeds else None
                            
                            if embed:
                                # Add logs field if not already present
                                if not any(field.name == "Draft Logs" for field in embed.fields):
                                    embed.add_field(
                                        name="Draft Logs",
                                        value=f"[View Draft Log]({logs_link})",
                                        inline=False
                                    )
                                    await message.edit(embed=embed)
                                    logger.info(f"Updated draft chat victory message with logs link")
                                    success = True
                                else:
                                    logger.info(f"Draft chat message already has logs field")
                            else:
                                logger.warning(f"No embed found in draft chat victory message")
                        except discord.NotFound:
                            logger.warning(f"Draft chat victory message not found: {draft_session.victory_message_id_draft_chat}")
                        except Exception as e:
                            logger.error(f"Error updating draft chat victory message: {e}")
                except discord.NotFound:
                    logger.warning(f"Draft chat channel not found: {draft_session.draft_chat_channel}")
            
            # Update results channel message
            if draft_session.victory_message_id_results_channel:
                try:
                    results_channel_name = "team-draft-results" if draft_session.session_type == "random" or draft_session.session_type == "staked" else "league-draft-results"
                    results_channel = discord.utils.get(guild.text_channels, name=results_channel_name)
                    
                    if results_channel:
                        try:
                            message = await results_channel.fetch_message(int(draft_session.victory_message_id_results_channel))
                            embed = message.embeds[0] if message.embeds else None
                            
                            if embed:
                                # Add logs field if not already present
                                if not any(field.name == "Draft Logs" for field in embed.fields):
                                    embed.add_field(
                                        name="Draft Logs",
                                        value=f"[View Draft Log]({logs_link})",
                                        inline=False
                                    )
                                    await message.edit(embed=embed)
                                    logger.info(f"Updated results channel victory message with logs link")
                                    success = True
                                else:
                                    logger.info(f"Results channel message already has logs field")
                            else:
                                logger.warning(f"No embed found in results channel victory message")
                        except discord.NotFound:
                            logger.warning(f"Results victory message not found: {draft_session.victory_message_id_results_channel}")
                        except Exception as e:
                            logger.error(f"Error updating results victory message: {e}")
                    else:
                        logger.warning(f"Results channel '{results_channel_name}' not found")
                except Exception as e:
                    logger.error(f"Error processing results channel: {e}")
            
            return success