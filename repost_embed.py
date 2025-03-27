#!/usr/bin/env python3
import asyncio
import json
import os
import urllib.parse
import argparse
import aiohttp
import discord
from dotenv import load_dotenv
from aiobotocore.session import get_session

# Try to load from .env file
load_dotenv()

# Command line parser setup
parser = argparse.ArgumentParser(description='Repost a draft embed for a specific draft ID.')
parser.add_argument('draft_id', help='The ID of the draft (without the DB prefix)')
parser.add_argument('guild_id', help='The Discord guild ID to post the embed to')
parser.add_argument('--cube', help='The name of the cube', default="Custom Cube")
parser.add_argument('--type', help='The session type (team or swiss)', default="team")
parser.add_argument('--token', help='Discord bot token (overrides environment variable)')

# Parse arguments early to get potential token
args, unknown = parser.parse_known_args()

# Get environment variables
DO_SPACES_REGION = os.getenv("DO_SPACES_REGION")
DO_SPACES_ENDPOINT = os.getenv("DO_SPACES_RAW_ENDPOINT")
DO_SPACES_KEY = os.getenv("DO_SPACES_KEY")
DO_SPACES_SECRET = os.getenv("DO_SPACES_SECRET")
DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET")
DISCORD_TOKEN = args.token if args.token else os.getenv("BOT_TOKEN")
MPT_API_KEY = os.getenv("MPT_API_KEY")

# Check for required tokens
if not DISCORD_TOKEN:
    print("ERROR: Discord token not found! Use --token parameter or set DISCORD_TOKEN environment variable.")
    exit(1)

if not MPT_API_KEY:
    print("ERROR: MagicProTools API key not found! Set MPT_API_KEY environment variable.")
    exit(1)

# Setup Discord client
intents = discord.Intents.default()
client = discord.Client(intents=intents)

async def fetch_draft_logs(draft_id, session_type="team"):
    """Fetch individual draft log files for all players."""
    print(f"Fetching draft logs for {draft_id}...")
    
    if not draft_id.startswith("DB"):
        draft_id = f"DB{draft_id}"
    
    folder = "swiss" if session_type.lower() == "swiss" else "team"
    logs_prefix = f"magic-draft-logs/draft_logs/{folder}/{draft_id}/"
    
    session = get_session()
    async with session.create_client(
        's3',
        region_name=DO_SPACES_REGION,
        endpoint_url=DO_SPACES_ENDPOINT,
        aws_access_key_id=DO_SPACES_KEY,
        aws_secret_access_key=DO_SPACES_SECRET
    ) as s3_client:
        try:
            # List all log files
            response = await s3_client.list_objects_v2(
                Bucket=DO_SPACES_BUCKET,
                Prefix=logs_prefix
            )
            
            log_files = [obj.get('Key') for obj in response.get('Contents', [])]
            if not log_files:
                print(f"No log files found for draft ID {draft_id}")
                return None
                
            print(f"Found {len(log_files)} draft log files.")
            
            # Download each log file
            player_logs = {}
            for log_file in log_files:
                try:
                    # Extract player info from filename
                    filename = log_file.split('/')[-1]
                    if filename.startswith('DraftLog_'):
                        user_id = filename[9:].split('.')[0]  # Extract user ID portion
                        
                        # Download the log file
                        file_response = await s3_client.get_object(
                            Bucket=DO_SPACES_BUCKET,
                            Key=log_file
                        )
                        log_content = await file_response['Body'].read()
                        log_text = log_content.decode('utf-8')
                        
                        # Parse player name from the log
                        player_name = "Unknown Player"
                        lines = log_text.split('\n')
                        for i, line in enumerate(lines):
                            if line == "Players:":
                                for j in range(i+1, min(i+10, len(lines))):
                                    if lines[j].startswith("-->"):
                                        player_name = lines[j][4:].strip()
                                        break
                        
                        # Store the log
                        player_logs[user_id] = {
                            "name": player_name,
                            "log": log_text
                        }
                        print(f"Downloaded log for {player_name} ({user_id})")
                except Exception as e:
                    print(f"Error downloading log file {log_file}: {e}")
            
            return player_logs
                
        except Exception as e:
            print(f"Error fetching draft logs: {e}")
            return None

async def submit_to_mpt_api(log_text):
    """Submit draft log directly to MagicProTools API."""
    try:
        # Create the API request
        url = "https://magicprotools.com/api/draft/add"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://draftmancer.com"
        }
        data = {
            "draft": log_text,
            "apiKey": MPT_API_KEY,
            "platform": "mtgadraft"
        }
        
        print(f"Submitting to MagicProTools API...")
        
        # Encode the data
        encoded_data = "&".join([f"{k}={urllib.parse.quote(v)}" for k, v in data.items()])
        
        # Make the request
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=encoded_data) as response:
                if response.status == 200:
                    json_response = await response.json()
                    if "url" in json_response and not json_response.get("error"):
                        print(f"Successfully submitted to MagicProTools API")
                        return json_response["url"]
                    else:
                        error = json_response.get('error', 'Unknown error')
                        print(f"MagicProTools API error: {error}")
                        print(f"Response: {json_response}")
                else:
                    print(f"MagicProTools API returned status {response.status}")
                    response_text = await response.text()
                    print(f"Response: {response_text}")
        
        return None  # Return None if unsuccessful
    except Exception as e:
        print(f"Error submitting to MagicProTools API: {e}")
        return None

async def generate_magicprotools_embed(player_logs, cube_name, draft_id, session_type="team"):
    """Generate a Discord embed with MagicProTools links for all drafters."""
    try:
        folder = "swiss" if session_type.lower() == "swiss" else "team"
        
        embed = discord.Embed(
            title=f"Draft Log: {draft_id}",
            description=f"View your draft on MagicProTools with the links below:\nDraft Type: {session_type.title()}, Cube: {cube_name}",
            color=0x3498db  # Blue color
        )
        
        # For each player, submit their log to the API and get a direct link
        for user_id, player_data in player_logs.items():
            player_name = player_data["name"]
            log_text = player_data["log"]
            
            print(f"Processing {player_name}'s log...")
            
            # Submit to MagicProTools API
            mpt_url = await submit_to_mpt_api(log_text)
            
            # Generate fallback URL
            txt_key = f"magic-draft-logs/draft_logs/{folder}/{draft_id}/DraftLog_{user_id}.txt"
            txt_url = f"https://{DO_SPACES_BUCKET}.{DO_SPACES_REGION}.digitaloceanspaces.com/{txt_key}"
            
            # Add field to embed
            if mpt_url:
                embed.add_field(
                    name=player_name,
                    value=f"[View on MagicProTools]({mpt_url})",
                    inline=False
                )
            else:
                # In case API fails, provide a message
                embed.add_field(
                    name=player_name,
                    value="⚠️ Failed to generate MagicProTools link",
                    inline=False
                )
        
        return embed
    except Exception as e:
        print(f"Error generating Discord embed: {e}")
        # Return a basic embed if there's an error
        return discord.Embed(
            title=f"Draft Log: {draft_id}",
            description="Error generating MagicProTools links. Check logs for details.",
            color=0xFF0000  # Red color
        )

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    
    try:
        # Parse arguments again to get all parameters
        args = parser.parse_args()
        
        draft_id = args.draft_id
        if draft_id.startswith("DB"):
            draft_id = draft_id[2:]  # Remove DB prefix if it exists
            
        guild_id = int(args.guild_id)
        cube_name = args.cube
        session_type = args.type
        
        await repost_embed(draft_id, guild_id, cube_name, session_type)
    except Exception as e:
        print(f"Error processing command line arguments: {e}")
    finally:
        # Exit after processing
        await client.close()

async def repost_embed(draft_id, guild_id, cube_name, session_type):
    """Fetch draft logs and post embed to specified guild."""
    try:
        print(f"Processing draft ID: {draft_id} for guild: {guild_id}")
        
        # Fetch the draft logs
        player_logs = await fetch_draft_logs(draft_id, session_type)
        if not player_logs:
            print(f"No draft logs found for ID: {draft_id}")
            return
            
        # Get the guild
        guild = client.get_guild(guild_id)
        if not guild:
            print(f"Could not find guild with ID {guild_id}")
            return
        
        # Find a channel named "draft-logs"
        draft_logs_channel = None
        for channel in guild.channels:
            if channel.name.lower() == "draft-logs" and hasattr(channel, "send"):
                draft_logs_channel = channel
                break
        
        if not draft_logs_channel:
            print(f"No 'draft-logs' channel found in guild {guild.name}")
            return
            
        # Generate the embed
        db_draft_id = f"DB{draft_id}"
        embed = await generate_magicprotools_embed(player_logs, cube_name, db_draft_id, session_type)
        
        # Send the embed
        await draft_logs_channel.send(embed=embed)
        print(f"Successfully sent embed to #{draft_logs_channel.name} in {guild.name}")
        
    except Exception as e:
        print(f"Error processing draft: {e}")

if __name__ == "__main__":
    try:
        # Run the Discord client
        asyncio.run(client.start(DISCORD_TOKEN))
    except KeyboardInterrupt:
        print("Script interrupted by user")
    except Exception as e:
        print(f"Error running script: {e}")