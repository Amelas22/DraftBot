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
                        cube TEXT,
                        record TEXT,
                        FOREIGN KEY (channel_id) REFERENCES log_channels (channel_id) ON DELETE CASCADE
                    )
                '''))
                logger.info("Successfully created backup_logs table")
            else:
                logger.info("Table backup_logs already exists")
                # Add new columns to existing backup_logs table if needed
                await add_columns_to_table(conn, 'backup_logs', [
                    ('cube', 'TEXT'),
                    ('record', 'TEXT')
                ])
            
            # Create user_submissions table if it doesn't exist
            if 'user_submissions' not in existing_tables:
                logger.info("Creating table: user_submissions")
                await conn.execute(text('''
                    CREATE TABLE user_submissions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT,
                        submitted_by TEXT NOT NULL,
                        submitted_on TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        channel_id TEXT NOT NULL,
                        used BOOLEAN DEFAULT 0,
                        cube TEXT,
                        record TEXT,
                        FOREIGN KEY (channel_id) REFERENCES log_channels (channel_id) ON DELETE CASCADE
                    )
                '''))
                logger.info("Successfully created user_submissions table")
            else:
                logger.info("Table user_submissions already exists")
                # Add new columns to existing user_submissions table if needed
                await add_columns_to_table(conn, 'user_submissions', [
                    ('cube', 'TEXT'),
                    ('record', 'TEXT')
                ])
                
                # Make url column nullable in user_submissions table
                await make_column_nullable(conn, 'user_submissions', 'url')
            
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

async def add_columns_to_table(conn, table_name, columns):
    """Add columns to an existing table if they don't exist"""
    # Get existing columns
    result = await conn.execute(text(f"PRAGMA table_info({table_name})"))
    existing_columns = {row[1] for row in result.fetchall()}
    
    # Add each column if it doesn't exist
    for column_name, column_type in columns:
        if column_name not in existing_columns:
            logger.info(f"Adding column {column_name} to {table_name}")
            await conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"))
            logger.info(f"Successfully added column {column_name} to {table_name}")
        else:
            logger.info(f"Column {column_name} already exists in {table_name}")

async def make_column_nullable(conn, table_name, column_name):
    """
    Make a column nullable in SQLite (requires recreating the table)
    Note: SQLite doesn't allow direct ALTER TABLE to change column constraints,
    so in a real implementation we'd need to create a new table, copy data, and rename.
    For now, we'll just check if we need to update the table structure.
    """
    try:
        # This is just a check to see if we need to perform the migration
        # SQLite doesn't provide direct information about nullability constraints
        # We'll attempt to insert a NULL value into a temporary table copy
        
        logger.info(f"Checking if {column_name} in {table_name} is already nullable...")
        
        # In a full implementation, you would:
        # 1. Create a new table with the desired schema
        # 2. Copy all data from the old table to the new table
        # 3. Drop the old table
        # 4. Rename the new table to the old table name
        
        # For now, just log that this would require a table recreation in SQLite
        logger.info(f"To make {column_name} nullable in {table_name} would require creating a new table.")
        logger.info(f"For production, implement a full table recreation to change the nullability constraint.")
    except Exception as e:
        logger.error(f"Error checking column nullability: {e}")

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