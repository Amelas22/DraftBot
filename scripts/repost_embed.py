#!/usr/bin/env python3
import asyncio
import os
import urllib.parse
import argparse
import aiohttp
import discord
from dotenv import load_dotenv

from models import DraftSession
from helpers.digital_ocean_helper import DigitalOceanHelper
from helpers.magicprotools_helper import MagicProtoolsHelper

# Try to load from .env file
load_dotenv()

# Command line parser setup
parser = argparse.ArgumentParser(description='Repost a draft embed for a specific draft ID.')
parser.add_argument('draft_id', help='The ID of the draft (without the DB prefix)')
parser.add_argument('guild_id', help='The Discord guild ID to post the embed to')
parser.add_argument('--cube', help='The name of the cube', default="Custom Cube")
parser.add_argument('--type', help='The session type (team or swiss)', default="team")
parser.add_argument('--token', help='Discord bot token (overrides environment variable)')

# Get environment variables
DO_SPACES_REGION = os.getenv("DO_SPACES_REGION")
DO_SPACES_ENDPOINT = os.getenv("DO_SPACES_RAW_ENDPOINT")
DO_SPACES_KEY = os.getenv("DO_SPACES_KEY")
DO_SPACES_SECRET = os.getenv("DO_SPACES_SECRET")
DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET")
DISCORD_TOKEN = os.getenv("BOT_TOKEN")
MPT_API_KEY = os.getenv("MPT_API_KEY")

# Setup Discord client
intents = discord.Intents.default()
client = discord.Client(intents=intents)


def player_logs_from_draft_data(draft_data):
    """Derive per-player MagicProTools text from a full draft-log JSON, replacing
    the removed per-player .txt archive as the recovery-script data source."""
    helper = MagicProtoolsHelper()
    logs = {}
    for user_id, user_data in draft_data.get("users", {}).items():
        logs[user_id] = {
            "name": user_data.get("userName", "Unknown Player"),
            "log_text": helper.convert_to_magicprotools_format(draft_data, user_id),
        }
    return logs


async def fetch_draft_logs(draft_id, session_type="team"):
    """Fetch the draft's JSON archive from DigitalOcean Spaces and derive
    per-player MagicProTools text from it."""
    print(f"Fetching draft logs for {draft_id}...")

    candidate_ids = [draft_id]
    if not draft_id.startswith("DB"):
        candidate_ids.append(f"DB{draft_id}")

    draft_session = None
    for candidate_id in candidate_ids:
        draft_session = await DraftSession.get_by_session_id(candidate_id)
        if draft_session:
            break

    if not draft_session:
        print(f"No draft session found for ID {draft_id}")
        return None

    object_key = draft_session.spaces_object_key
    if not object_key:
        print(f"Draft session {draft_id} has no spaces_object_key; JSON archive unavailable")
        return None

    draft_data = await DigitalOceanHelper().download_json(object_key)
    if not draft_data:
        print(f"Failed to download draft log JSON from {object_key}")
        return None

    return player_logs_from_draft_data(draft_data)

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
        embed = discord.Embed(
            title=f"Draft Log: {draft_id}",
            description=f"View your draft on MagicProTools with the links below:\nDraft Type: {session_type.title()}, Cube: {cube_name}",
            color=0x3498db  # Blue color
        )

        # For each player, submit their log to the API and get a direct link
        for user_id, player_data in player_logs.items():
            player_name = player_data["name"]
            log_text = player_data["log_text"]

            print(f"Processing {player_name}'s log...")

            # Submit to MagicProTools API
            mpt_url = await submit_to_mpt_api(log_text)

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
    # Parse arguments early to get potential token
    args, unknown = parser.parse_known_args()

    if args.token:
        DISCORD_TOKEN = args.token

    # Check for required tokens
    if not DISCORD_TOKEN:
        print("ERROR: Discord token not found! Use --token parameter or set DISCORD_TOKEN environment variable.")
        exit(1)

    if not MPT_API_KEY:
        print("ERROR: MagicProTools API key not found! Set MPT_API_KEY environment variable.")
        exit(1)

    try:
        # Run the Discord client
        asyncio.run(client.start(DISCORD_TOKEN))
    except KeyboardInterrupt:
        print("Script interrupted by user")
    except Exception as e:
        print(f"Error running script: {e}")