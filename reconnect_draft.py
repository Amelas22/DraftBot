"""
Script to reconnect to a specific draft session that was disconnected.
Usage: python reconnect_draft.py
"""

import asyncio
import os
import sys
import discord
from dotenv import load_dotenv
from sqlalchemy.future import select
from loguru import logger

# Add parent directory to path if needed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import local modules
from datacollections import DraftLogManager
from session import AsyncSessionLocal, DraftSession

# Load environment variables
load_dotenv()

# Set up logger
logger.remove()
logger.add(sys.stderr, level="INFO")
logger.add("reconnect.log", rotation="10 MB", level="INFO")

# Discord bot setup (if needed)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = discord.Client(intents=intents)

# Target draft ID
TARGET_DRAFT_ID = "C3TKCESZ"

async def get_draft_session():
    """Retrieve the draft session information from the database."""
    async with AsyncSessionLocal() as db_session:
        # Try to find the session by draft_id
        stmt = select(DraftSession).filter(DraftSession.draft_id == TARGET_DRAFT_ID)
        result = await db_session.execute(stmt)
        session = result.scalar_one_or_none()
        
        if not session:
            logger.error(f"Draft session with draft_id {TARGET_DRAFT_ID} not found!")
            return None
        
        logger.info(f"Found draft session: {session.session_id}")
        return session

async def reconnect_to_draft():
    """Main function to reconnect to the draft."""
    logger.info(f"Starting reconnection to draft session with draft_id: {TARGET_DRAFT_ID}")
    
    # Retrieve the draft session from the database
    draft_session = await get_draft_session()
    if not draft_session:
        return
    
    # Check if we need to log in to Discord
    if not draft_session.guild_id:
        logger.info("No guild_id found in session, proceeding without Discord client")
        discord_client = None
        guild_id = None
    else:
        logger.info(f"Guild ID found: {draft_session.guild_id}, initializing Discord client")
        # Set up Discord bot
        try:
            await bot.login(os.getenv("DISCORD_TOKEN"))
            discord_client = bot
            guild_id = int(draft_session.guild_id)
            logger.info("Successfully logged in to Discord")
        except Exception as e:
            logger.error(f"Failed to log in to Discord: {e}")
            discord_client = None
            guild_id = None
    
    # Create the draft log manager
    draft_log_manager = DraftLogManager(
        session_id=draft_session.session_id,
        draft_link=draft_session.draft_link,
        draft_id=TARGET_DRAFT_ID,
        session_type=draft_session.session_type or "team",  # Default to team if not specified
        cube=draft_session.cube or "Unknown",  # Default to Unknown if not specified
        discord_client=discord_client,
        guild_id=guild_id
    )
    
    logger.info(f"Created DraftLogManager for session {draft_session.session_id}")
    
    # Skip initial delay (since we're reconnecting)
    draft_log_manager.first_connection = False
    
    # Connect to the websocket and start monitoring
    try:
        logger.info("Starting draft session monitoring")
        await draft_log_manager.keep_draft_session_alive()
        logger.info("Draft session monitoring completed")
    except Exception as e:
        logger.error(f"Error during draft session monitoring: {e}")
    
    # Disconnect from Discord if needed
    if discord_client:
        await discord_client.close()

if __name__ == "__main__":
    try:
        asyncio.run(reconnect_to_draft())
    except KeyboardInterrupt:
        logger.info("Script interrupted by user")
    except Exception as e:
        logger.error(f"Unhandled exception: {e}")