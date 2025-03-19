<<<<<<< HEAD
# db_migrate_role_cooldowns.py
=======
>>>>>>> fb0896d71cededd3715c42dc17f628b0c497d96e
import asyncio
import logging
from sqlalchemy import text

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Database URL - should match your existing configuration
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"  # Update this if your database is in a different file

<<<<<<< HEAD
async def create_role_ping_cooldowns_table():
    """Create the role_ping_cooldowns table using raw SQL, safer for SQLite"""
=======
async def add_last_update_time_column():
    """Add last_update_time column to messages table if it doesn't exist"""
>>>>>>> fb0896d71cededd3715c42dc17f628b0c497d96e
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(DATABASE_URL, echo=True)
    
    try:
        async with engine.begin() as conn:
<<<<<<< HEAD
            # Check if the table already exists
            result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='role_ping_cooldowns'"))
            table_exists = result.scalar()
            
            if not table_exists:
                # Create the table with pure SQL (SQLite-safe approach)
                create_table_sql = """
                CREATE TABLE role_ping_cooldowns (
                    id VARCHAR(64) PRIMARY KEY,
                    role_id VARCHAR(64) NOT NULL,
                    guild_id VARCHAR(64) NOT NULL,
                    last_ping_time FLOAT DEFAULT 0.0,
                    cooldown_period FLOAT DEFAULT 3600.0,
                    is_managed BOOLEAN DEFAULT 1
                );
                """
                await conn.execute(text(create_table_sql))
                logger.info("Created role_ping_cooldowns table successfully")
            else:
                logger.info("role_ping_cooldowns table already exists")
                
            # Check if all columns exist (in case we need to add columns to an existing table)
            if table_exists:
                columns_query = """
                PRAGMA table_info(role_ping_cooldowns);
=======
            # Check if the column already exists
            try:
                columns_query = """
                PRAGMA table_info(messages);
>>>>>>> fb0896d71cededd3715c42dc17f628b0c497d96e
                """
                result = await conn.execute(text(columns_query))
                columns = result.fetchall()
                
<<<<<<< HEAD
                column_names = [col[1] for col in columns]
                
                # Check for missing columns and add them if needed
                if 'is_managed' not in column_names:
                    await conn.execute(text("ALTER TABLE role_ping_cooldowns ADD COLUMN is_managed BOOLEAN DEFAULT 1"))
                    logger.info("Added is_managed column to role_ping_cooldowns table")
                
                # Add similar checks for other columns if needed in the future

    except Exception as e:
        logger.error(f"Error creating role_ping_cooldowns table: {e}")
=======
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
>>>>>>> fb0896d71cededd3715c42dc17f628b0c497d96e
        raise
    finally:
        await engine.dispose()

async def migrate_database():
<<<<<<< HEAD
    """Execute all migration steps"""
    logger.info("Starting database migration for role ping cooldowns feature")
    
    try:
        # Create the role_ping_cooldowns table
        await create_role_ping_cooldowns_table()
        
=======
    """Execute the migration step"""
    logger.info("Starting database migration to add last_update_time column")
    
    try:
        await add_last_update_time_column()
>>>>>>> fb0896d71cededd3715c42dc17f628b0c497d96e
        logger.info("Database migration completed successfully")
    except Exception as e:
        logger.error(f"Database migration failed: {e}")

if __name__ == "__main__":
    asyncio.run(migrate_database())
