import asyncio
from datetime import datetime, timedelta
from sqlalchemy.future import select
from session import AsyncSessionLocal, DraftSession
from datacollections import DraftLogManager
from loguru import logger
from dotenv import load_dotenv
import os

load_dotenv()

async def reconnect_recent_draft_sessions(discord_client):
    """
    Reconnect to recent active draft sessions after bot restart.
    Only reconnects to sessions that:
    1. Don't have data received yet
    2. Have a teams_start_time that isn't NULL
    3. Started within the last 5 hours
    4. Are NOT winston drafts
    
    Args:
        discord_client: The existing bot client instance
    """
    logger.info("Reconnecting to recent active draft sessions after bot restart...")
    
    # Calculate the cutoff time (5 hours ago)
    current_time = datetime.now()
    five_hours_ago = current_time - timedelta(hours=5)
    
    logger.info(f"Looking for non-winston sessions that started between {five_hours_ago} and {current_time}")
    
    async with AsyncSessionLocal() as db_session:
        # Query for draft sessions that meet our criteria
        stmt = select(DraftSession).filter(
            DraftSession.data_received == False,  # Draft log data not yet received
            DraftSession.teams_start_time.isnot(None),  # teams_start_time is not NULL
            DraftSession.teams_start_time >= five_hours_ago,  # Started within the last 5 hours
            DraftSession.teams_start_time <= current_time,  # Started before now (sanity check)
            DraftSession.session_type != "winston"  # Exclude winston drafts
        )
        
        result = await db_session.execute(stmt)
        active_sessions = result.scalars().all()
        
        logger.info(f"Found {len(active_sessions)} recent active draft sessions to reconnect")
        
        # Create tasks for each session
        tasks = []
        for session in active_sessions:
            # Skip if missing required fields
            if not all([session.session_id, session.draft_id, session.session_type, session.cube]):
                logger.warning(f"Skipping session {session.session_id}: Missing required fields")
                continue
                
            # Calculate how long ago the session started
            time_since_start = current_time - session.teams_start_time
            hours_since_start = time_since_start.total_seconds() / 3600
            
            logger.info(f"Session {session.session_id} (type: {session.session_type}) started {hours_since_start:.1f} hours ago")
            
            # Create draft link for reference
            draft_link = f"https://draftmancer.com/draft/DB{session.draft_id}"
            
            try:
                # Create a new DraftLogManager for this session
                manager = DraftLogManager(
                    session_id=session.session_id,
                    draft_link=draft_link,
                    draft_id=session.draft_id,
                    session_type=session.session_type,
                    cube=session.cube,
                    discord_client=discord_client,
                    guild_id=int(session.guild_id) if session.guild_id else None
                )
                
                # Skip the first connection delay by setting this to False
                manager.first_connection = False
                
                # Create and add the task
                task = asyncio.create_task(manager.keep_draft_session_alive())
                tasks.append(task)
                
                logger.info(f"Created reconnection task for draft ID: DB{session.draft_id} (type: {session.session_type}, started {hours_since_start:.1f} hours ago)")
            except Exception as e:
                logger.error(f"Error creating reconnection task for session {session.session_id}: {e}")
        
        # Return the tasks without awaiting them
        return tasks