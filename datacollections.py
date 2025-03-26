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
            print(f"Successfully connected to the websocket for draft_id: DB{self.draft_id}")

        @self.sio.event
        async def connect_error(data):
            print(f"Connection to the websocket failed for draft_id: DB{self.draft_id}")

        @self.sio.event
        async def disconnect():
            print(f"Disconnected from the websocket for draft_id: DB{self.draft_id}")

    async def keep_draft_session_alive(self):
        keep_running = True
        # if self.first_connection:
        #     await asyncio.sleep(1800)
        #     self.first_connection = False
        while keep_running:
            try:
                await self.sio.connect(
                    f'wss://draftmancer.com?userID=DraftBot&sessionID=DB{self.draft_id}&userName=DraftBot',
                    transports='websocket',
                    wait_timeout=10)
                
                while True:
                    if self.fetch_attempts >= 20 or self.connection_attempts >= 20:
                        print(f"Exceeded maximum attempts for {self.draft_id}, stopping attempts and disconnecting.")
                        keep_running = False
                        break

                    data_fetched = await self.fetch_draft_log_data()
                    if data_fetched:
                        print(f"Draft log data fetched and saved for {self.draft_id}, staying connected for 3 hours and 15 minutes.")
                        
                        # Keep connection alive for 3 hours and 15 minutes (11700 seconds)
                        # Send a ping every 2 minutes (120 seconds)
                        remaining_time = 11700  
                        ping_interval = 120  
                        
                        while remaining_time > 0:
                            try:
                                await self.sio.emit('ping')  
                                await asyncio.sleep(min(ping_interval, remaining_time))  
                                remaining_time -= min(ping_interval, remaining_time)
                            except socketio.exceptions.ConnectionError:
                                print(f"Connection to {self.draft_link} closed during waiting period, reconnecting...")
                                break  
                        
                        print(f"Time period elapsed for {self.draft_id}, closing connection.")
                        await self.sio.disconnect()
                        return
                    else:
                        print(f"{self.draft_id} log data not available attempt {self.fetch_attempts}, retrying in 5 minutes...")
                        await asyncio.sleep(300)  # Retry every 5 minutes

                    try:
                        await self.sio.emit('ping')  # Send a ping to keep the connection alive
                        await asyncio.sleep(120)  # Send a ping every 2 minutes
                    except socketio.exceptions.ConnectionError:
                        print(f"Connection to {self.draft_link} closed, retrying...")
                        self.connection_attempts += 1
                        break

            except Exception as e:
                print(f"Error connecting to {self.draft_link}: {e}")
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
                            
                            next_fetch_time = datetime.now(pytz.utc) + timedelta(seconds=7500)
                            next_fetch_time_eastern = next_fetch_time.astimezone(eastern)
                            print(f"Draft log data for {self.draft_id} has no picks, retrying at {next_fetch_time_eastern.strftime('%Y-%m-%d %H:%M:%S %Z')}")
                            await asyncio.sleep(7500)  # Wait for 2 hours and 5 minutes
                            self.first_delay = True
                            self.fetch_attempts += 1
                            return await self.fetch_draft_log_data()  # Retry fetching the data
                        elif self.first_delay and not first_user_picks:
                            print(f"Draft log data for {self.draft_id} has no picks, retrying in 5 minutes")
                            await asyncio.sleep(300)  # Wait for 5 minutes
                            self.fetch_attempts += 1
                            return await self.fetch_draft_log_data()
                        elif first_user_picks:
                            await self.save_draft_log_data(draft_data)
                            return True
                    else:
                        print(f"Failed to fetch draft log data: status code {response.status}")
                        self.fetch_attempts += 1
                        return False
            except Exception as e:
                print(f"Exception while fetching draft log data: {e}")
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
                    # Generate MagicProTools links after successful upload
                    if self.discord_client and self.guild_id:
                        await self.send_magicprotools_embed(draft_data)
                elif draft_session:
                    draft_session.draft_data = draft_data
                    print(f"Draft log data saved in database for {self.draft_id}; SessionID: {self.session_id}")
                else:
                    print(f"Draft session {self.session_id} not found in the database")
            await db_session.commit()

    async def send_magicprotools_embed(self, draft_data):
        """Find draft-logs channel and send the embed if found."""
        try:
            # Find the guild
            guild = self.discord_client.get_guild(self.guild_id)
            if not guild:
                print(f"Could not find guild with ID {self.guild_id}")
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
                await draft_logs_channel.send(embed=embed)
                print(f"Sent MagicProTools links to #{draft_logs_channel.name} in {guild.name}")
            else:
                print(f"No 'draft-logs' channel found in guild {guild.name}, skipping embed message")
        except Exception as e:
            print(f"Error sending MagicProTools embed: {e}")

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
                print(f"Draft log data uploaded to DigitalOcean Space: {object_name}")
                
                # If upload successful, also generate and upload MagicProTools format logs
                await self.process_draft_logs_for_magicprotools(draft_data, s3_client, DO_SPACES_BUCKET)
                
                return True
            except Exception as e:
                print(f"Error uploading to DigitalOcean Space: {e}")
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
                
                print(f"MagicProTools format log for {user_name} uploaded: {txt_key}")
                
            print(f"All MagicProTools format logs generated and uploaded for draft {session_id}")
            return True
        except Exception as e:
            print(f"Error generating MagicProTools format logs: {e}")
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
        """Generate a Discord embed with MagicProTools links for all drafters."""
        try:
            import discord  # Import locally to avoid issues if Discord isn't available
            
            DO_SPACES_REGION = os.getenv("DO_SPACES_REGION")
            DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET")
            session_id = draft_data.get("sessionID")
            folder = "swiss" if self.session_type == "swiss" else "team"
            
            embed = discord.Embed(
                title=f"Draft Log: {session_id}",
                description=f"Import your draft to MagicProTools with the links below:\nDraft Type: {self.session_type.title()}, Cube: {self.cube}",
                color=0x3498db  # Blue color
            )
            
            for user_id, user_data in draft_data["users"].items():
                user_name = user_data["userName"]
                
                # Generate URLs
                txt_key = f"draft_logs/{folder}/{session_id}/DraftLog_{user_id}.txt"
                txt_url = f"https://{DO_SPACES_BUCKET}.{DO_SPACES_REGION}.digitaloceanspaces.com/{txt_key}"
                mpt_url = f"https://magicprotools.com/draft/import?url={urllib.parse.quote(txt_url)}"
                
                # Direct API method 
                mpt_api_key = os.getenv("MPT_API_KEY")  
                if mpt_api_key:
                    try:
                        direct_mpt_url = await self.submit_to_mpt_api(user_id, draft_data, mpt_api_key)
                        if direct_mpt_url:
                            # If API call successful, use the direct URL
                            embed.add_field(
                                name=user_name,
                                value=f"[View Raw Log]({txt_url}) | [View on MagicProTools]({direct_mpt_url})",
                                inline=False
                            )
                            continue
                    except Exception as e:
                        print(f"Error submitting to MagicProTools API for {user_name}: {e}")
                        # Fall back to URL method
            
            return embed
        except Exception as e:
            print(f"Error generating Discord embed: {e}")
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
                            print(f"Successfully submitted to MagicProTools API for user {user_id}")
                            return json_response["url"]
                        else:
                            print(f"MagicProTools API error: {json_response.get('error', 'Unknown error')}")
                    else:
                        print(f"MagicProTools API returned status {response.status}")
            
            return None  # Return None if unsuccessful
        except Exception as e:
            print(f"Error submitting to MagicProTools API: {e}")
            return None