from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from contextlib import asynccontextmanager
import logging
from sqlalchemy import text

# Import Base for database initialization
from .models_base import Base

# Set up logging
logging.basicConfig(level=logging.WARNING)
logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)

# Database URL - you might want to move this to a config file later
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"

# Create engine
# - timeout=30: Wait up to 30 seconds for locks (Python-side fallback)
# - check_same_thread=False: Required for async
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={
        "timeout": 30,
        "check_same_thread": False
    }
)

# Create session factory
AsyncSessionLocal = sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession
)

def get_session_factory():
    """
    Factory function to get the session maker.
    This allows tests to inject a different session factory.
    """
    return AsyncSessionLocal

@asynccontextmanager
async def db_session():
    """Context manager for database sessions with automatic commit/rollback"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception as e:
            await session.rollback()
            logging.error(f"Database error: {e}")
            raise

async def init_db():
    """Initialize the database, create tables if they don't exist"""
    async with engine.begin() as conn:
        # WAL mode allows concurrent reads during writes - persists to db file
        await conn.execute(text("PRAGMA journal_mode=WAL"))

        await conn.run_sync(Base.metadata.create_all)

    # Run any migrations needed after initialization
    await run_migrations()

async def run_migrations():
    """Run necessary migrations for existing tables"""
    # Example: Add guild_id to player_stats if needed
    await migrate_player_stats()
    
    # You can add more migrations here as needed

async def migrate_player_stats():
    """Add guild_id column to player_stats and populate existing entries"""
    async with engine.begin() as conn:
        # Check if the column already exists
        try:
            await conn.execute(text("SELECT guild_id FROM player_stats LIMIT 1"))
            logging.info("guild_id column already exists in player_stats")
        except Exception as e:
            if "no such column: guild_id" in str(e):
                logging.info("Adding guild_id column to player_stats...")
                # Add the guild_id column
                await conn.execute(text("ALTER TABLE player_stats ADD COLUMN guild_id VARCHAR(64)"))
                
                # Set all existing entries to the special guild ID
                special_guild_id = "336345350535118849"
                await conn.execute(text(f"UPDATE player_stats SET guild_id = '{special_guild_id}'"))
                logging.info(f"Updated all existing entries with guild_id = {special_guild_id}")
                
                # Update primary key constraint
                try:
                    # SQLite doesn't support ALTER TABLE ADD CONSTRAINT so we need to recreate the table
                    await conn.execute(text("""
                        CREATE TABLE player_stats_new (
                            player_id VARCHAR(64) NOT NULL,
                            guild_id VARCHAR(64) NOT NULL,
                            display_name VARCHAR(128),
                            drafts_participated INTEGER DEFAULT 0,
                            games_won INTEGER DEFAULT 0,
                            games_lost INTEGER DEFAULT 0,
                            elo_rating FLOAT DEFAULT 1200.0,
                            true_skill_mu FLOAT DEFAULT 25.0,
                            true_skill_sigma FLOAT DEFAULT 8.333,
                            PRIMARY KEY (player_id, guild_id)
                        )
                    """))
                    
                    # Copy data
                    await conn.execute(text("""
                        INSERT INTO player_stats_new
                        SELECT player_id, guild_id, display_name, drafts_participated, 
                               games_won, games_lost, elo_rating, true_skill_mu, true_skill_sigma
                        FROM player_stats
                    """))
                    
                    # Replace tables
                    await conn.execute(text("DROP TABLE player_stats"))
                    await conn.execute(text("ALTER TABLE player_stats_new RENAME TO player_stats"))
                    
                    logging.info("Updated primary key constraint for player_stats")
                except Exception as e:
                    logging.error(f"Error updating primary key constraint: {e}")

async def ensure_guild_id_in_tables():
    """Ensure all relevant tables have a guild_id column"""
    tables_to_check = [
        'draft_sessions', 
        'match_results',
        'player_stats',
        'messages'
    ]
    
    async with engine.begin() as conn:
        for table in tables_to_check:
            try:
                # Check if guild_id column exists
                query = f"SELECT guild_id FROM {table} LIMIT 1"
                await conn.execute(text(query))
            except Exception as e:
                if "no such column: guild_id" in str(e):
                    # Add guild_id column
                    query = f"ALTER TABLE {table} ADD COLUMN guild_id VARCHAR(64)"
                    await conn.execute(text(query))
                    logging.info(f"Added guild_id column to {table}")

async def execute_query(query_func):
    """Execute a query function within a database session
    
    Example usage:
    result = await execute_query(
        lambda session: session.execute(select(MyModel).filter_by(id=123))
    )
    """
    async with db_session() as session:
        return await query_func(session)
