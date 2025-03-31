#!/usr/bin/env python
"""
Script to backfill pack_first_picks for existing drafts.
This script will:
1. Find drafts with data_received = True but no pack_first_picks data
2. Download the draft data from DigitalOcean Spaces
3. Process the data to extract first picks
4. Update the pack_first_picks column in the database
"""

import asyncio
import json
import os
import sys
import boto3
import urllib.parse
import requests
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
logger.add("backfill_first_picks.log", rotation="10 MB", level="INFO")

# Load environment variables
load_dotenv()

# Timestamp for March 16th, 2025 at 1pm ET
CUTOFF_DATE = 1742144400

# DigitalOcean Spaces configuration
DO_SPACES_REGION = os.getenv("DO_SPACES_REGION", "nyc3")
DO_SPACES_BUCKET = os.getenv("DO_SPACES_BUCKET", "magic-draft-logs")
DO_SPACES_KEY = os.getenv("DO_SPACES_KEY")
DO_SPACES_SECRET = os.getenv("DO_SPACES_SECRET")

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

def get_pack_first_picks(draft_data, user_id):
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
        logger.error(f"Error getting first picks for user {user_id}: {e}")
        return {}

async def process_draft_session(draft_session):
    """Process a single draft session to extract and store first picks."""
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
        
        # Extract first picks for each user
        draftmancer_user_picks = {}
        for user_id, user_data in draft_data["users"].items():
            user_pack_picks = get_pack_first_picks(draft_data, user_id)
            draftmancer_user_picks[user_id] = user_pack_picks
        
        # Convert Draftmancer user IDs to Discord user IDs
        discord_user_pack_picks = {}
        
        # Get list of Discord user IDs from sign_ups
        if draft_session.sign_ups:
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
        
        # Update the database
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                # Get the draft session (again, to ensure we have the latest version)
                stmt = select(DraftSession).filter(DraftSession.session_id == draft_session.session_id)
                result = await db_session.execute(stmt)
                updated_draft_session = result.scalar_one_or_none()
                
                if updated_draft_session:
                    updated_draft_session.pack_first_picks = discord_user_pack_picks
                    await db_session.commit()
                    logger.info(f"Updated pack_first_picks for session {draft_session.session_id} with {len(discord_user_pack_picks)} user entries")
                    return True
                else:
                    logger.warning(f"Draft session {draft_session.session_id} not found during update")
                    return False
                
    except Exception as e:
        logger.error(f"Error processing draft session {draft_session.session_id}: {e}")
        return False

async def backfill_pack_first_picks():
    """Main function to backfill pack_first_picks for qualifying drafts."""
    logger.info("Starting backfill of pack_first_picks")
    
    async with AsyncSessionLocal() as db_session:
        # Query draft sessions that match our criteria
        cutoff_date = datetime.fromtimestamp(CUTOFF_DATE)
        query = select(DraftSession).where(
            and_(
                or_(
                    DraftSession.session_type == "random",
                    DraftSession.session_type == "staked"
                ),
                DraftSession.teams_start_time > cutoff_date,
                DraftSession.data_received == True,
                DraftSession.pack_first_picks == None
            )
        )
        
        result = await db_session.execute(query)
        drafts_to_process = result.scalars().all()
        
        logger.info(f"Found {len(drafts_to_process)} draft sessions to process")
        
        success_count = 0
        for draft in drafts_to_process:
            success = await process_draft_session(draft)
            if success:
                success_count += 1
                # Give some time between operations to prevent rate limiting
                await asyncio.sleep(0.5)
        
        logger.info(f"Backfill completed. Successfully processed {success_count} out of {len(drafts_to_process)} drafts")

if __name__ == "__main__":
    asyncio.run(backfill_pack_first_picks())