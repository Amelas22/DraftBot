import asyncio
from datetime import datetime, timedelta
from sqlalchemy.future import select
from session import AsyncSessionLocal, DraftSession
from services.draft_setup_manager import (
    DraftSetupManager, DEFAULT_PACKS_PER_PLAYER, DEFAULT_CARDS_PER_PACK,
)
from loguru import logger

async def reconnect_draft_setup_sessions(discord_client):
    """
    Reconnect to sessions that need draft setup after bot restart.
    Running each connection sequentially with a 1-second delay between them.
    """
    logger.info("Reconnecting to recent draft setup sessions after bot restart...")
    
    # Calculate the cutoff time (5 hours ago)
    current_time = datetime.now()
    five_hours_ago = current_time - timedelta(hours=5)
    
    logger.info(f"Looking for draft sessions needing setup created between {five_hours_ago} and {current_time}")
    
    # List to track successful connections for monitoring
    successful_connections = []
    
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
        
        # Instead of creating tasks, we'll return manager objects that will be processed one by one
        managers = []
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
                    cube_id=session.cube,
                    guild_id=session.guild_id,
                    packs_per_player=getattr(session, 'packs_per_player', None) or DEFAULT_PACKS_PER_PLAYER,
                    cards_per_pack=getattr(session, 'cards_per_pack', None) or DEFAULT_CARDS_PER_PACK
                )
                
                # Add to our list
                managers.append(manager)
                
                logger.info(f"Created setup manager for draft ID: DB{session.draft_id} (type: {session.session_type})")
            except Exception as e:
                logger.error(f"Error creating setup manager for session {session.session_id}: {e}")
        
        return managers