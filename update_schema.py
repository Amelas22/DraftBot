import asyncio
import logging
from sqlalchemy import text, MetaData, Table, Column, Integer, String, DateTime
from sqlalchemy.ext.asyncio import create_async_engine
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Database URL - should match your existing configuration
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"

async def create_leaderboard_messages_table():
    """Create the leaderboard_messages table if it doesn't exist"""
    engine = create_async_engine(DATABASE_URL, echo=True)
    
    try:
        async with engine.begin() as conn:
            # Check if the table exists
            result = await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='leaderboard_messages'"
            ))
            
            table_exists = result.scalar() is not None
            
            if not table_exists:
                # Create the table
                metadata = MetaData()
                
                # Define the table structure
                leaderboard_messages = Table(
                    'leaderboard_messages', 
                    metadata,
                    Column('id', Integer, primary_key=True, autoincrement=True),
                    Column('guild_id', String(64), nullable=False),
                    Column('channel_id', String(64), nullable=False),
                    Column('message_id', String(64), nullable=False),
                    Column('last_updated', DateTime, default=datetime.now)
                )
                
                # Create the table
                await conn.run_sync(metadata.create_all, tables=[leaderboard_messages])
                logger.info("Created leaderboard_messages table")
            else:
                logger.info("leaderboard_messages table already exists")
                
    except Exception as e:
        logger.error(f"Error creating leaderboard_messages table: {e}")
        raise
    finally:
        await engine.dispose()

async def migrate_database():
    """Execute all migration steps"""
    logger.info("Starting database migration to add leaderboard_messages table")
    
    try:
        # Create the leaderboard_messages table
        await create_leaderboard_messages_table()
        
        logger.info("Database migration completed successfully")
    except Exception as e:
        logger.error(f"Database migration failed: {e}")

if __name__ == "__main__":
    asyncio.run(migrate_database())