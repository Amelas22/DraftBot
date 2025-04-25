import asyncio
import logging
from sqlalchemy import text, MetaData, Table, Column, String, Integer, Boolean, DateTime, ForeignKey
from sqlalchemy.ext.asyncio import create_async_engine
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Database URL - should match your existing configuration
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"

async def create_draft_logs_tables():
    """Create tables for the draft logs system"""
    engine = create_async_engine(DATABASE_URL, echo=True)
    
    try:
        async with engine.begin() as conn:
            # Check if tables already exist
            result = await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('log_channels', 'backup_logs', 'user_submissions', 'post_schedules')"
            ))
            
            existing_tables = {row[0] for row in result.fetchall()}
            
            # Create log_channels table if it doesn't exist
            if 'log_channels' not in existing_tables:
                logger.info("Creating table: log_channels")
                await conn.execute(text('''
                    CREATE TABLE log_channels (
                        channel_id TEXT PRIMARY KEY,
                        guild_id TEXT NOT NULL,
                        last_post TIMESTAMP,
                        time_zone TEXT DEFAULT 'UTC'
                    )
                '''))
                logger.info("Successfully created log_channels table")
            else:
                logger.info("Table log_channels already exists")
            
            # Create post_schedules table if it doesn't exist
            if 'post_schedules' not in existing_tables:
                logger.info("Creating table: post_schedules")
                await conn.execute(text('''
                    CREATE TABLE post_schedules (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id TEXT NOT NULL,
                        post_time TEXT NOT NULL,
                        FOREIGN KEY (channel_id) REFERENCES log_channels (channel_id) ON DELETE CASCADE
                    )
                '''))
                logger.info("Successfully created post_schedules table")
            else:
                logger.info("Table post_schedules already exists")
            
            # Create backup_logs table if it doesn't exist
            if 'backup_logs' not in existing_tables:
                logger.info("Creating table: backup_logs")
                await conn.execute(text('''
                    CREATE TABLE backup_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT NOT NULL,
                        added_by TEXT NOT NULL,
                        added_on TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        channel_id TEXT NOT NULL,
                        used BOOLEAN DEFAULT 0,
                        FOREIGN KEY (channel_id) REFERENCES log_channels (channel_id) ON DELETE CASCADE
                    )
                '''))
                logger.info("Successfully created backup_logs table")
            else:
                logger.info("Table backup_logs already exists")
            
            # Create user_submissions table if it doesn't exist
            if 'user_submissions' not in existing_tables:
                logger.info("Creating table: user_submissions")
                await conn.execute(text('''
                    CREATE TABLE user_submissions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT NOT NULL,
                        submitted_by TEXT NOT NULL,
                        submitted_on TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        channel_id TEXT NOT NULL,
                        used BOOLEAN DEFAULT 0,
                        FOREIGN KEY (channel_id) REFERENCES log_channels (channel_id) ON DELETE CASCADE
                    )
                '''))
                logger.info("Successfully created user_submissions table")
            else:
                logger.info("Table user_submissions already exists")
            
            # Check if the post_time column exists in log_channels and migrate if needed
            if 'log_channels' in existing_tables:
                result = await conn.execute(text("PRAGMA table_info(log_channels)"))
                columns = {row[1] for row in result.fetchall()}
                
                if 'post_time' in columns:
                    logger.info("Migrating post_time data to post_schedules table")
                    # Get all channels with post_time
                    result = await conn.execute(text(
                        "SELECT channel_id, post_time FROM log_channels WHERE post_time IS NOT NULL"
                    ))
                    channels_with_post_time = result.fetchall()
                    
                    # Insert post_time values into post_schedules
                    for channel_id, post_time in channels_with_post_time:
                        await conn.execute(text(
                            "INSERT INTO post_schedules (channel_id, post_time) VALUES (:channel_id, :post_time)"
                        ), {"channel_id": channel_id, "post_time": post_time})
                    
                    # Remove post_time column from log_channels
                    # SQLite doesn't support dropping columns directly, so we need to recreate the table
                    # But for this migration, we'll just leave it and ignore it in the code
                    logger.info("Migration complete - post_time column will be ignored")
                
    except Exception as e:
        logger.error(f"Error creating draft logs tables: {e}")
        raise
    finally:
        await engine.dispose()

async def migrate_database():
    """Execute all migration steps"""
    logger.info("Starting database migration to create draft logs tables with schedule support")
    
    try:
        # Create the new tables
        await create_draft_logs_tables()
        
        logger.info("Database migration completed successfully")
    except Exception as e:
        logger.error(f"Database migration failed: {e}")

if __name__ == "__main__":
    asyncio.run(migrate_database())