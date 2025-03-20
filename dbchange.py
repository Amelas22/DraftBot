import asyncio
import logging
from sqlalchemy import text

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Database URL - should match your existing configuration
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"

async def add_is_capped_column():
    """Add is_capped column to stake_info table"""
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(DATABASE_URL, echo=True)
    
    try:
        async with engine.begin() as conn:
            # First check if the stake_info table exists
            result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='stake_info'"))
            table_exists = result.scalar()
            
            if not table_exists:
                logger.warning("stake_info table does not exist. No migration needed.")
                return
            
            # Check if the is_capped column already exists
            columns_query = """
            PRAGMA table_info(stake_info);
            """
            result = await conn.execute(text(columns_query))
            columns = result.fetchall()
            
            column_names = [col[1] for col in columns]
            
            # Add the is_capped column if it doesn't exist
            if 'is_capped' not in column_names:
                # SQLite doesn't support ADD COLUMN with DEFAULT for existing rows in one statement,
                # so we need to add the column first, then update existing rows
                await conn.execute(text("ALTER TABLE stake_info ADD COLUMN is_capped BOOLEAN"))
                logger.info("Added is_capped column to stake_info table")
                
                # Now set the default value for all existing rows
                await conn.execute(text("UPDATE stake_info SET is_capped = 1"))
                logger.info("Set default value (TRUE) for is_capped column on all existing records")
            else:
                logger.info("is_capped column already exists in stake_info table")

    except Exception as e:
        logger.error(f"Error adding is_capped column to stake_info table: {e}")
        raise
    finally:
        await engine.dispose()

async def migrate_database():
    """Execute all migration steps"""
    logger.info("Starting database migration for stake capping feature")
    
    try:
        # Add the is_capped column to stake_info table
        await add_is_capped_column()
        
        logger.info("Database migration completed successfully")
    except Exception as e:
        logger.error(f"Database migration failed: {e}")

if __name__ == "__main__":
    asyncio.run(migrate_database())