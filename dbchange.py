# db_migrate_staked_draft.py
import asyncio
import logging
from sqlalchemy import text

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Database URL - should match your existing configuration
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"

async def add_min_stake_column():
    """Add min_stake column to DraftSession table if it doesn't exist"""
    from sqlalchemy.ext.asyncio import create_async_engine
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
                
                # Check if min_stake column exists
                has_min_stake = any(col[1] == 'min_stake' for col in columns)
                
                if not has_min_stake:
                    # Column doesn't exist, add it
                    await conn.execute(text("ALTER TABLE draft_sessions ADD COLUMN min_stake INTEGER DEFAULT 10"))
                    logger.info("Added min_stake column to draft_sessions table")
                else:
                    logger.info("min_stake column already exists")
            except Exception as e:
                logger.error(f"Error checking for min_stake column: {e}")
                raise
    except Exception as e:
        logger.error(f"Error adding min_stake column: {e}")
        raise
    finally:
        await engine.dispose()

async def create_stake_info_table():
    """Create the stake_info table using raw SQL, safer for SQLite"""
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(DATABASE_URL, echo=True)
    
    try:
        async with engine.begin() as conn:
            # Check if the table already exists
            result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='stake_info'"))
            table_exists = result.scalar()
            
            if not table_exists:
                # Create the table with pure SQL (SQLite-safe approach)
                create_table_sql = """
                CREATE TABLE stake_info (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id VARCHAR(64),
                    player_id VARCHAR(64) NOT NULL,
                    max_stake INTEGER NOT NULL,
                    assigned_stake INTEGER,
                    opponent_id VARCHAR(64),
                    FOREIGN KEY (session_id) REFERENCES draft_sessions(session_id)
                );
                """
                await conn.execute(text(create_table_sql))
                logger.info("Created stake_info table successfully")
            else:
                logger.info("stake_info table already exists")
    except Exception as e:
        logger.error(f"Error creating stake_info table: {e}")
        raise
    finally:
        await engine.dispose()

async def migrate_database():
    """Execute all migration steps"""
    logger.info("Starting database migration for staked draft feature")
    
    try:
        # Step 1: Add min_stake column to DraftSession (this should always be safe)
        await add_min_stake_column()
        
        # Step 2: Create the stake_info table
        await create_stake_info_table()
        
        logger.info("Database migration completed successfully")
    except Exception as e:
        logger.error(f"Database migration failed: {e}")

if __name__ == "__main__":
    asyncio.run(migrate_database())