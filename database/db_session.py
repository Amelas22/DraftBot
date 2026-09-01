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

    # No schema edits here. Alembic owns the schema: draftbot.service runs
    # `alembic upgrade head` before the bot starts, and a startup routine that
    # also alters tables can only undo it. One did, for months -- it re-added
    # match_results.guild_id after the migration that dropped it, which is why
    # the live schema and the migration history disagreed.

async def execute_query(query_func):
    """Execute a query function within a database session
    
    Example usage:
    result = await execute_query(
        lambda session: session.execute(select(MyModel).filter_by(id=123))
    )
    """
    async with db_session() as session:
        return await query_func(session)
