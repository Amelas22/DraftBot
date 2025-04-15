import asyncio
import logging
from sqlalchemy import text, MetaData, Table, Column, String
from sqlalchemy.ext.asyncio import create_async_engine

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Database URL - should match your existing configuration
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"

async def add_status_message_id_column():
    """Add status_message_id column to the draft_sessions table"""
    engine = create_async_engine(DATABASE_URL, echo=True)
    
    try:
        async with engine.begin() as conn:
            # Check if the table exists
            result = await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='draft_sessions'"
            ))
            
            table_exists = result.scalar() is not None
            
            if not table_exists:
                logger.error("draft_sessions table does not exist. Make sure the database is properly initialized.")
                return
            
            # Get current columns in the table
            result = await conn.execute(text(
                "PRAGMA table_info(draft_sessions)"
            ))
            columns = {row[1] for row in result.fetchall()}
            
            # Add status_message_id column if it doesn't exist
            if 'status_message_id' not in columns:
                logger.info("Adding column: status_message_id")
                await conn.execute(text(
                    "ALTER TABLE draft_sessions ADD COLUMN status_message_id VARCHAR"
                ))
                logger.info("Successfully added status_message_id column to draft_sessions table")
            else:
                logger.info("Column status_message_id already exists")
                
    except Exception as e:
        logger.error(f"Error adding status_message_id column to draft_sessions table: {e}")
        raise
    finally:
        await engine.dispose()

async def migrate_database():
    """Execute all migration steps"""
    logger.info("Starting database migration to add status_message_id column")
    
    try:
        # Add the new column
        await add_status_message_id_column()
        
        logger.info("Database migration completed successfully")
    except Exception as e:
        logger.error(f"Database migration failed: {e}")

if __name__ == "__main__":
    asyncio.run(migrate_database())