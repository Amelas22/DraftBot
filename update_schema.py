import asyncio
import logging
from sqlalchemy import text

# Configure logging
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Database URL - should match your existing configuration
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"

async def add_draftmancer_role_users_column_to_draft_sessions():
    """Add magicprotools_links column to draft_sessions table"""
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(DATABASE_URL, echo=True)
    
    try:
        async with engine.begin() as conn:
            # Check if the draftmancer_role_users column already exists
            result = await conn.execute(text(
                "PRAGMA table_info(draft_sessions)"
            ))
            columns = {row[1]: row for row in result.fetchall()}
            
            # Check if the column needs to be added
            if 'draftmancer_role_users' not in columns:
                # For SQLite, JSON is stored as TEXT
                alter_table_sql = """
                ALTER TABLE draft_sessions 
                ADD COLUMN draftmancer_role_users JSON;
                """
                await conn.execute(text(alter_table_sql))
                logger.info("Added draftmancer_role_users column to draft_sessions table")
            else:
                logger.info("draftmancer_role_users column already exists in draft_sessions table")          
            
                
    except Exception as e:
        logger.error(f"Error adding draftmancer_role_users column to draft_sessions table: {e}")
        raise
    finally:
        await engine.dispose()

async def migrate_database():
    """Execute all migration steps"""
    logger.info("Starting database migration to add draftmancer_role_users column to draft_sessions")
    
    try:
        # Add column to the draft_sessions table
        await add_draftmancer_role_users_column_to_draft_sessions()
        
        logger.info("Database migration completed successfully")
    except Exception as e:
        logger.error(f"Database migration failed: {e}")

if __name__ == "__main__":
    asyncio.run(migrate_database())