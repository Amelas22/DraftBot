# add_missing_column.py
import asyncio
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Database URL - should match your existing configuration
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"

async def add_live_draft_message_id_column():
    """Add live_draft_message_id column to DraftSession table"""
    engine = create_async_engine(DATABASE_URL, echo=True)
    
    try:
        async with engine.begin() as conn:
            # Check if the column already exists
            try:
                columns_query = """
                PRAGMA table_info(draft_sessions);
                """
                result = await conn.execute(text(columns_query))
                columns = result.fetchall()
                
                # Check if live_draft_message_id column exists
                has_column = any(col[1] == 'live_draft_message_id' for col in columns)
                
                if not has_column:
                    # Column doesn't exist, add it
                    await conn.execute(text("ALTER TABLE draft_sessions ADD COLUMN live_draft_message_id VARCHAR(64)"))
                    logger.info("Added live_draft_message_id column to draft_sessions table")
                else:
                    logger.info("live_draft_message_id column already exists")
            except Exception as e:
                logger.error(f"Error checking for live_draft_message_id column: {e}")
                raise
    except Exception as e:
        logger.error(f"Error adding live_draft_message_id column: {e}")
        raise
    finally:
        await engine.dispose()

async def main():
    """Run the migration"""
    logger.info("Starting migration to add missing column")
    try:
        await add_live_draft_message_id_column()
        logger.info("Migration completed successfully")
    except Exception as e:
        logger.error(f"Migration failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())