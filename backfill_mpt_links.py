#!/usr/bin/env python
"""
Script to backfill magicprotools_links for existing drafts with enhanced debugging.
"""

import asyncio
import json
import os
import sys
import boto3
import urllib.parse
import requests
import aiohttp
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy import and_, or_, select
from loguru import logger

# Add parent directory to path if needed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import local modules
from session import AsyncSessionLocal, DraftSession

# Set up logger
logger.remove()
logger.add(sys.stderr, level="INFO")
logger.add("backfill_mpt_links.log", rotation="10 MB", level="INFO")

# Load environment variables
load_dotenv()

# Timestamp for March 16th, 2025 at 1pm ET
CUTOFF_DATE = 1742144400

# DigitalOcean Spaces configuration
DO_SPACES_REGION = os.getenv("DO_SPACES_REGION", "nyc3")
DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET", "magic-draft-logs")
DO_SPACES_KEY = os.getenv("DO_SPACES_KEY")
DO_SPACES_SECRET = os.getenv("DO_SPACES_SECRET")
MPT_API_KEY = os.getenv("MPT_API_KEY")  # For MagicProTools API

# Create an S3 client for listing objects
s3_client = boto3.client(
    's3',
    region_name=DO_SPACES_REGION,
    endpoint_url=f"https://{DO_SPACES_REGION}.digitaloceanspaces.com",
    aws_access_key_id=DO_SPACES_KEY,
    aws_secret_access_key=DO_SPACES_SECRET
)

# Base URL for direct file access
DO_BASE_URL = f"https://{DO_SPACES_BUCKET}.{DO_SPACES_REGION}.digitaloceanspaces.com"

# Check MPT API Key
if not MPT_API_KEY:
    logger.error("MPT_API_KEY environment variable is not set! Cannot submit to MagicProTools API.")
else:
    logger.info(f"MPT_API_KEY is set (length: {len(MPT_API_KEY)})")

def find_draft_json_file(folder, cube, draft_id):
    """Find a JSON file containing the draft_id in the specified folder."""
    try:
        # List all objects in the folder
        prefix = f"{DO_SPACES_BUCKET}/{folder}/"
        logger.info(f"Listing objects with prefix: {prefix}")
        
        try:
            paginator = s3_client.get_paginator('list_objects_v2')
            matching_files = []
            
            for page in paginator.paginate(Bucket=DO_SPACES_BUCKET, Prefix=prefix):
                if 'Contents' in page:
                    for obj in page['Contents']:
                        key = obj['Key']
                        # Look for files that contain the draft_id and end with .json
                        # Also check for the DB prefix that sometimes gets added
                        if (draft_id in key or f"DB{draft_id}" in key) and key.endswith('.json'):
                            # Also check if the cube name is in the path
                            if cube.lower() in key.lower():
                                matching_files.append(key)
                                logger.info(f"Found matching file: {key}")
            
            if not matching_files:
                logger.warning(f"No matching files found for draft_id={draft_id}, cube={cube} in folder={folder}")
                return None
            
            # If multiple matches, get the most recent one (assuming it's the longest filename or alphabetically last)
            file_key = sorted(matching_files)[-1]
            logger.info(f"Using file: {file_key}")
            
            return file_key
            
        except Exception as e:
            logger.error(f"Error listing objects: {e}")
            return None
            
    except Exception as e:
        logger.error(f"Error in find_draft_json_file: {e}")
        return None

def download_draft_data(file_key):
    """Download draft data from the specified file key."""
    try:
        # Construct direct URL for the file
        url = f"{DO_BASE_URL}/{file_key}"
        logger.info(f"Downloading from URL: {url}")
        
        response = requests.get(url)
        if response.status_code == 200:
            logger.info(f"Successfully downloaded draft data from: {url}")
            return response.json()
        else:
            logger.error(f"Failed to download draft data. Status code: {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"Error downloading draft data: {e}")
        return None

def convert_to_magicprotools_format(draft_log, user_id):
    """Convert a draft log JSON to MagicProTools format for a specific user."""
    try:
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
    except Exception as e:
        logger.error(f"Error converting to MagicProTools format for user {user_id}: {e}")
        return None

async def submit_to_mpt_api(mpt_format, draft_data, user_id):
    """Submit draft data directly to MagicProTools API."""
    # Check if we have an API key
    if not MPT_API_KEY:
        logger.error("Cannot submit to MagicProTools API: MPT_API_KEY is not set")
        return None
    
    # Check if we have valid format data
    if not mpt_format:
        logger.error(f"Cannot submit to MagicProTools API: Invalid format data for user {user_id}")
        return None
    
    logger.info(f"Preparing to submit draft to MagicProTools API for user {user_id}")
    
    try:
        # Create the API request
        url = "https://magicprotools.com/api/draft/add"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://draftmancer.com"
        }
        data = {
            "draft": mpt_format,
            "apiKey": MPT_API_KEY,
            "platform": "mtgadraft"
        }
        
        # Encode the data
        encoded_data = "&".join([f"{k}={urllib.parse.quote(v)}" for k, v in data.items()])
        
        # Log that we're submitting
        logger.info(f"Submitting to MagicProTools API for user {user_id}")
        
        # Make the request
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=encoded_data) as response:
                logger.info(f"MagicProTools API response status: {response.status}")
                
                if response.status == 200:
                    response_text = await response.text()
                    logger.info(f"API response text: {response_text[:100]}...")  # Log first 100 chars
                    
                    try:
                        json_response = json.loads(response_text)
                        if "url" in json_response and not json_response.get("error"):
                            logger.info(f"Successfully submitted to MagicProTools API for user {user_id}")
                            logger.info(f"URL received: {json_response['url']}")
                            return json_response["url"]
                        else:
                            error_msg = json_response.get("error", "Unknown error")
                            logger.warning(f"MagicProTools API error: {error_msg}")
                    except json.JSONDecodeError:
                        logger.error(f"Failed to parse API response as JSON: {response_text[:100]}...")
                else:
                    logger.warning(f"MagicProTools API returned status {response.status}")
        
        return None  # Return None if unsuccessful
    except Exception as e:
        logger.error(f"Error submitting to MagicProTools API: {e}")
        return None

async def process_draft_session(draft_session):
    """Process a single draft session to generate MPT links."""
    try:
        logger.info(f"Processing draft session {draft_session.session_id}, draft_id {draft_session.draft_id}")
        
        # Check if we have the draft_data stored in the database first
        if draft_session.draft_data:
            logger.info(f"Found draft data in database for session {draft_session.session_id}")
            draft_data = draft_session.draft_data
        else:
            # Get draft data from DigitalOcean Spaces
            logger.info(f"Getting draft data from DigitalOcean for session {draft_session.session_id}")
            
            # Determine folder (team or swiss)
            folder = "swiss" if draft_session.session_type == "swiss" else "team"
            
            # Get the cube name, defaulting to "Unknown" if not available
            cube_name = draft_session.cube or "Unknown"
            
            # Find the file
            file_key = find_draft_json_file(folder, cube_name, draft_session.draft_id)
            
            if not file_key:
                logger.warning(f"Could not find draft data file for session {draft_session.session_id}")
                return False
            
            # Download the file
            draft_data = download_draft_data(file_key)
        
        if not draft_data:
            logger.warning(f"Could not retrieve draft data for session {draft_session.session_id}")
            return False
        
        # Log the number of users in the draft data
        user_count = len(draft_data["users"]) if "users" in draft_data else 0
        logger.info(f"Draft data contains {user_count} users")
        
        # Generate MagicProTools links for each user
        magicprotools_links = {}
        
        # Get list of Discord user IDs from sign_ups
        if draft_session.sign_ups:
            discord_ids = list(draft_session.sign_ups.keys())
            logger.info(f"Sign_ups contains {len(discord_ids)} Discord IDs")
            
            # Print the first few sign_ups entries for debugging
            debug_sign_ups = {}
            for i, (k, v) in enumerate(draft_session.sign_ups.items()):
                if i < 3:  # Just show first 3 for brevity
                    debug_sign_ups[k] = v
            logger.info(f"Sample sign_ups: {json.dumps(debug_sign_ups)}")
            
            # Sort users by seat number
            sorted_users = sorted(
                [(user_id, user_data) for user_id, user_data in draft_data["users"].items()],
                key=lambda item: item[1].get("seatNum", 999)
            )
            logger.info(f"Sorted {len(sorted_users)} users by seat number")
            
            # Map Draftmancer user IDs to Discord user IDs based on draft seat order
            logger.info("Beginning mapping of Draftmancer users to Discord IDs")
            
            for idx, (draft_user_id, user_data) in enumerate(sorted_users):
                user_name = user_data.get("userName", "Unknown")
                logger.info(f"Processing user {idx+1}/{len(sorted_users)}: {user_name} (ID: {draft_user_id})")
                
                if idx < len(discord_ids):
                    discord_id = discord_ids[idx]
                    logger.info(f"Mapped to Discord ID: {discord_id}")
                    
                    # Generate and store MagicProTools links
                    # First convert to MagicProTools format
                    logger.info(f"Converting draft data to MagicProTools format for user {user_name}")
                    mpt_format = convert_to_magicprotools_format(draft_data, draft_user_id)
                    
                    if not mpt_format:
                        logger.error(f"Failed to convert to MagicProTools format for user {user_name}")
                        continue
                    
                    # Get user name
                    discord_name = None
                    
                    # Get Discord display name if available
                    sign_up_info = draft_session.sign_ups.get(discord_id)
                    if sign_up_info:
                        if isinstance(sign_up_info, dict) and "name" in sign_up_info:
                            discord_name = sign_up_info["name"]
                        else:
                            discord_name = sign_up_info
                    
                    logger.info(f"Discord display name: {discord_name or 'Unknown'}")
                    
                    # Submit to MagicProTools API
                    logger.info(f"Attempting to submit to MagicProTools API for user {user_name}")
                    mpt_url = await submit_to_mpt_api(mpt_format, draft_data, draft_user_id)
                    
                    if mpt_url:
                        # Store the link
                        magicprotools_links[discord_id] = {
                            "name": discord_name or user_name,
                            "link": mpt_url
                        }
                        logger.info(f"Successfully generated and stored MPT link for user {user_name}")
                    else:
                        logger.warning(f"Failed to generate MPT link for user {user_name}")
                else:
                    logger.warning(f"No matching Discord ID for user {user_name} at position {idx}")
        else:
            logger.warning(f"No sign_ups data for session {draft_session.session_id}")
        
        # Show the number of links we found
        logger.info(f"Generated {len(magicprotools_links)} MagicProTools links for session {draft_session.session_id}")
        
        # Update the database
        async with AsyncSessionLocal() as db_session:
            # Get the draft session (again, to ensure we have the latest version)
            stmt = select(DraftSession).filter(DraftSession.session_id == draft_session.session_id)
            result = await db_session.execute(stmt)
            updated_draft_session = result.scalar_one_or_none()
            
            if updated_draft_session and magicprotools_links:
                logger.info(f"Updating magicprotools_links in database for session {draft_session.session_id}")
                
                # Update magicprotools_links
                updated_draft_session.magicprotools_links = magicprotools_links
                
                # Add the updated object back to the session
                db_session.add(updated_draft_session)
                
                # Commit the changes
                await db_session.commit()
                
                logger.info(f"Successfully committed magicprotools_links to database for session {draft_session.session_id}")
                return True
            else:
                if not updated_draft_session:
                    logger.warning(f"Draft session {draft_session.session_id} not found during update")
                else:
                    logger.warning(f"No MagicProTools links were generated for session {draft_session.session_id}")
                return False
                
    except Exception as e:
        logger.error(f"Error processing draft session {draft_session.session_id}: {e}")
        return False

async def backfill_mpt_links():
    """Main function to backfill magicprotools_links for qualifying drafts."""
    logger.info("Starting backfill of MagicProTools links")
    
    # Check if we have an API key - if not, exit early
    if not MPT_API_KEY:
        logger.error("MPT_API_KEY is not set. Cannot proceed with backfill.")
        return
    
    async with AsyncSessionLocal() as db_session:
        # Query draft sessions that match our criteria (needs MPT links)
        cutoff_date = datetime.fromtimestamp(CUTOFF_DATE)
        query = select(DraftSession).where(
            and_(
                or_(
                    DraftSession.session_type == "random",
                    DraftSession.session_type == "staked"
                ),
                DraftSession.teams_start_time > cutoff_date,
                DraftSession.data_received == True,
                DraftSession.magicprotools_links == None
            )
        )
        
        result = await db_session.execute(query)
        drafts_to_process = result.scalars().all()
        
        logger.info(f"Found {len(drafts_to_process)} draft sessions to process")
        
        # Limit to just 3 for testing
        test_limit = int(os.getenv("TEST_LIMIT", "0"))
        if test_limit > 0:
            logger.info(f"TEST MODE: Limiting to {test_limit} drafts")
            drafts_to_process = drafts_to_process[:test_limit]
        
        success_count = 0
        for i, draft in enumerate(drafts_to_process):
            logger.info(f"Processing draft {i+1}/{len(drafts_to_process)}: {draft.session_id}")
            success = await process_draft_session(draft)
            if success:
                success_count += 1
                # Give some time between operations to prevent rate limiting
                await asyncio.sleep(0.5)
        
        logger.info(f"Backfill completed. Successfully processed {success_count} out of {len(drafts_to_process)} drafts")

if __name__ == "__main__":
    asyncio.run(backfill_mpt_links())