# db_migrate_player_preferences.py
import asyncio
import logging
from sqlalchemy import text

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Database URL - should match your existing configuration
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"

async def create_player_preferences_table():
    """Create the player_preferences table using raw SQL, safer for SQLite"""
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(DATABASE_URL, echo=True)
    
    try:
        async with engine.begin() as conn:
            # Check if the table already exists
            result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='player_preferences'"))
            table_exists = result.scalar()
            
            if not table_exists:
                # Create the table with pure SQL (SQLite-safe approach)
                create_table_sql = """
                CREATE TABLE player_preferences (
                    id VARCHAR(128) PRIMARY KEY,
                    player_id VARCHAR(64) NOT NULL,
                    guild_id VARCHAR(64) NOT NULL, 
                    is_bet_capped BOOLEAN DEFAULT 1,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
                await conn.execute(text(create_table_sql))
                logger.info("Created player_preferences table successfully")
                
                # Create an index for faster lookups
                index_sql = """
                CREATE INDEX idx_player_guild 
                ON player_preferences(player_id, guild_id);
                """
                await conn.execute(text(index_sql))
                logger.info("Created index on player_id and guild_id")
            else:
                logger.info("player_preferences table already exists")
                
    except Exception as e:
        logger.error(f"Error creating player_preferences table: {e}")
        raise
    finally:
        await engine.dispose()

async def migrate_database():
    """Execute all migration steps"""
    logger.info("Starting database migration for player preferences")
    
    try:
        # Create the player_preferences table
        await create_player_preferences_table()
        
        logger.info("Database migration completed successfully")
    except Exception as e:
        logger.error(f"Database migration failed: {e}")

if __name__ == "__main__":
    asyncio.run(migrate_database())