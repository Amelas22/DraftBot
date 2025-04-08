import asyncio
from datetime import datetime, timedelta
from sqlalchemy.future import select
from session import AsyncSessionLocal, DraftSession
from datacollections import DraftLogManager
from services.draft_setup_manager import DraftSetupManager
from loguru import logger
from config import get_draftmancer_draft_url

async def reconnect_draft_setup_sessions(discord_client):
    """
    Reconnect to sessions that need draft setup after bot restart.
    Only reconnects to sessions that:
    1. Have session_stage as NULL (teams not created)
    2. Were created within the last 5 hours
    """
    logger.info("Reconnecting to recent draft setup sessions after bot restart...")
    
    # Calculate the cutoff time (5 hours ago)
    current_time = datetime.now()
    five_hours_ago = current_time - timedelta(hours=5)
    
    logger.info(f"Looking for draft sessions needing setup created between {five_hours_ago} and {current_time}")
    
    async with AsyncSessionLocal() as db_session:
        # Query for draft sessions that need setup
        stmt = select(DraftSession).filter(
            DraftSession.session_stage.is_(None),  # teams have not been formed
            DraftSession.draft_start_time >= five_hours_ago,  # Created within the last 5 hours
            DraftSession.draft_start_time <= current_time  
        )
        
        result = await db_session.execute(stmt)
        active_sessions = result.scalars().all()
        
        logger.info(f"Found {len(active_sessions)} recent draft sessions needing setup")
        
        # Create tasks for each session
        tasks = []
        for session in active_sessions:
            # Skip if missing required fields
            if not all([session.session_id, session.draft_id, session.cube]):
                logger.warning(f"Skipping session {session.session_id}: Missing required fields")
                continue
                
            # Calculate how long ago the session was created
            time_since_creation = current_time - session.draft_start_time
            hours_since_creation = time_since_creation.total_seconds() / 3600
            
            logger.info(f"Session {session.session_id} (type: {session.session_type}) created {hours_since_creation:.1f} hours ago")
            
            try:
                # Create a new DraftSetupManager for this session
                manager = DraftSetupManager(
                    session_id=session.session_id,
                    draft_id=session.draft_id,
                    cube_id=session.cube
                )
                
                # Create and add the task
                task = asyncio.create_task(manager.keep_connection_alive())
                tasks.append(task)
                
                logger.info(f"Created setup reconnection task for draft ID: DB{session.draft_id} (type: {session.session_type})")
            except Exception as e:
                logger.error(f"Error creating setup reconnection task for session {session.session_id}: {e}")
        
        return tasks

async def reconnect_recent_draft_sessions(discord_client):
    """
    Reconnect to recent active draft sessions to pull logs after restart.
    Only reconnects to sessions that:
    1. Don't have data received yet
    2. Have a teams_start_time that isn't NULL
    3. Started within the last 5 hours
    4. Are NOT winston drafts
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
            DraftSession.teams_start_time.isnot(None),  # teams have been formed
            DraftSession.teams_start_time >= five_hours_ago,  # Started within the last 5 hours
            DraftSession.teams_start_time <= current_time, 
            DraftSession.session_stage == "pairings",
            DraftSession.session_type != "winston"  
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
            draft_link = get_draftmancer_draft_url(session.draft_id)
            
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
                
                manager.first_connection = False  # Skip the 15-minute wait in keep_draft_session_alive
                manager.first_delay = True # Skip the 90-minute wait in fetch_draft_log_data
                
                # Create and add the task
                task = asyncio.create_task(manager.keep_draft_session_alive())
                tasks.append(task)
                
                logger.info(f"Created reconnection task for draft ID: DB{session.draft_id} (type: {session.session_type}, started {hours_since_start:.1f} hours ago)")
            except Exception as e:
                logger.error(f"Error creating reconnection task for session {session.session_id}: {e}")
        
        return tasks