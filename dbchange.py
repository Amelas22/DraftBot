import asyncio
import logging
from sqlalchemy import text

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Database URL - should match your existing configuration
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"

async def add_timestamp_column_to_player_stats():
    """Add last_draft_timestamp column to player_stats table"""
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(DATABASE_URL, echo=True)
    
    try:
        async with engine.begin() as conn:
            # Check if the last_draft_timestamp column already exists
            result = await conn.execute(text(
                "PRAGMA table_info(player_stats)"
            ))
            columns = {row[1]: row for row in result.fetchall()}
            
            if 'last_draft_timestamp' not in columns:
                logger.info("last_draft_timestamp column needs to be added")
                
                # Add the column if it doesn't exist
                alter_table_sql = """
                ALTER TABLE player_stats 
                ADD COLUMN last_draft_timestamp DATETIME;
                """
                await conn.execute(text(alter_table_sql))
                logger.info("Added last_draft_timestamp column to player_stats table")
            else:
                logger.info("last_draft_timestamp column already exists in player_stats table")
                
    except Exception as e:
        logger.error(f"Error adding last_draft_timestamp column to player_stats table: {e}")
        raise
    finally:
        await engine.dispose()

async def migrate_database():
    """Execute all migration steps"""
    logger.info("Starting database migration to add timestamp column to player_stats")
    
    try:
        # Add column to the player_stats table
        await add_timestamp_column_to_player_stats()
        
        logger.info("Database migration completed successfully")
    except Exception as e:
        logger.error(f"Database migration failed: {e}")

if __name__ == "__main__":
    asyncio.run(migrate_database())