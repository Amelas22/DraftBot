import asyncio
import logging
from sqlalchemy import text

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Database URL - should match your existing configuration
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"  # Update this if your database is in a different file

async def add_last_update_time_column():
    """Add last_update_time column to messages table if it doesn't exist"""
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(DATABASE_URL, echo=True)
    
    try:
        async with engine.begin() as conn:
            # Check if the column already exists
            try:
                columns_query = """
                PRAGMA table_info(messages);
                """
                result = await conn.execute(text(columns_query))
                columns = result.fetchall()
                
                # Check if last_update_time column exists
                has_last_update_time = any(col[1] == 'last_update_time' for col in columns)
                
                if not has_last_update_time:
                    # Column doesn't exist, add it
                    await conn.execute(text("ALTER TABLE messages ADD COLUMN last_update_time REAL DEFAULT 0.0"))
                    logger.info("Added last_update_time column to messages table")
                else:
                    logger.info("last_update_time column already exists")
            except Exception as e:
                logger.error(f"Error checking for last_update_time column: {e}")
                raise
    except Exception as e:
        logger.error(f"Error adding last_update_time column: {e}")
        raise
    finally:
        await engine.dispose()

async def migrate_database():
    """Execute the migration step"""
    logger.info("Starting database migration to add last_update_time column")
    
    try:
        await add_last_update_time_column()
        logger.info("Database migration completed successfully")
    except Exception as e:
        logger.error(f"Database migration failed: {e}")

if __name__ == "__main__":
    asyncio.run(migrate_database())