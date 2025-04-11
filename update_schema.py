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

async def add_timeframe_columns():
    """Add columns for timeframe view message IDs and timeframe settings to the leaderboard_messages table"""
    engine = create_async_engine(DATABASE_URL, echo=True)
    
    try:
        async with engine.begin() as conn:
            # Check if the table exists
            result = await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='leaderboard_messages'"
            ))
            
            table_exists = result.scalar() is not None
            
            if not table_exists:
                logger.error("leaderboard_messages table does not exist. Please run create_leaderboard_messages_table first.")
                return
            
            # Get current columns in the table
            result = await conn.execute(text(
                "PRAGMA table_info(leaderboard_messages)"
            ))
            columns = {row[1] for row in result.fetchall()}
            
            # Define new columns to add
            new_columns = [
                # View message ID columns
                ('draft_record_view_message_id', String(64)),
                ('match_win_view_message_id', String(64)),
                ('drafts_played_view_message_id', String(64)),
                ('time_vault_and_key_view_message_id', String(64)),
                
                # Timeframe setting columns
                ('draft_record_timeframe', String(20)),
                ('match_win_timeframe', String(20)),
                ('drafts_played_timeframe', String(20)),
                ('time_vault_and_key_timeframe', String(20))
            ]
            
            # Add each column if it doesn't exist
            for column_name, column_type in new_columns:
                if column_name not in columns:
                    logger.info(f"Adding column: {column_name}")
                    await conn.execute(text(
                        f"ALTER TABLE leaderboard_messages ADD COLUMN {column_name} {column_type}"
                    ))
                else:
                    logger.info(f"Column {column_name} already exists")
                
    except Exception as e:
        logger.error(f"Error adding columns to leaderboard_messages table: {e}")
        raise
    finally:
        await engine.dispose()

async def migrate_database():
    """Execute all migration steps"""
    logger.info("Starting database migration to add timeframe columns")
    
    try:
        # Add the new columns
        await add_timeframe_columns()
        
        logger.info("Database migration completed successfully")
    except Exception as e:
        logger.error(f"Database migration failed: {e}")

if __name__ == "__main__":
    asyncio.run(migrate_database())