import asyncio
import logging
from sqlalchemy import text

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Database URL - should match your existing configuration
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"

async def add_logs_columns_to_draft_sessions():
    """Add logs_channel_id and logs_message_id columns to draft_sessions table"""
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(DATABASE_URL, echo=True)
    
    try:
        async with engine.begin() as conn:
            # Check if the logs_channel_id column already exists
            result = await conn.execute(text(
                "PRAGMA table_info(draft_sessions)"
            ))
            columns = {row[1]: row for row in result.fetchall()}
            
            columns_to_add = []
            if 'logs_channel_id' not in columns:
                columns_to_add.append(('logs_channel_id', 'VARCHAR(64)'))
                logger.info("logs_channel_id column needs to be added")
            
            if 'logs_message_id' not in columns:
                columns_to_add.append(('logs_message_id', 'VARCHAR(64)'))
                logger.info("logs_message_id column needs to be added")
            
            # Add the columns if they don't exist
            if columns_to_add:
                for column_name, column_type in columns_to_add:
                    alter_table_sql = f"""
                    ALTER TABLE draft_sessions 
                    ADD COLUMN {column_name} {column_type};
                    """
                    await conn.execute(text(alter_table_sql))
                    logger.info(f"Added {column_name} column to draft_sessions table")
            else:
                logger.info("All required columns already exist in draft_sessions table")
                
    except Exception as e:
        logger.error(f"Error adding columns to draft_sessions table: {e}")
        raise
    finally:
        await engine.dispose()

async def migrate_database():
    """Execute all migration steps"""
    logger.info("Starting database migration to add logs columns to draft_sessions")
    
    try:
        # Add columns to the draft_sessions table
        await add_logs_columns_to_draft_sessions()
        
        logger.info("Database migration completed successfully")
    except Exception as e:
        logger.error(f"Database migration failed: {e}")

if __name__ == "__main__":
    asyncio.run(migrate_database())