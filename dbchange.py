import asyncio
import time
from sqlalchemy import Column, Float, select, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
import sqlalchemy as sa
from loguru import logger
from session import DATABASE_URL

async def add_last_activity_column():
    """Add last_activity column to the messages table if it doesn't exist."""
    # Create engine and session
    engine = create_async_engine(DATABASE_URL)
    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with engine.begin() as conn:
        # Check if the column already exists
        try:
            result = await conn.execute(text("SELECT last_activity FROM messages LIMIT 1"))
            logger.info("Column 'last_activity' already exists.")
            return
        except Exception:
            logger.info("Column 'last_activity' does not exist. Adding it now.")
            
            # Add the column
            await conn.execute(
                text("ALTER TABLE messages ADD COLUMN last_activity FLOAT NOT NULL DEFAULT 0.0")
            )
            logger.info("Column 'last_activity' has been added successfully.")

    # Initialize the column with the current timestamp for all existing records
    current_time = time.time()
    async with async_session() as session:
        async with session.begin():
            await session.execute(
                text(f"UPDATE messages SET last_activity = {current_time}")
            )
            logger.info(f"Initialized last_activity to {current_time} for all existing records.")

if __name__ == "__main__":
    asyncio.run(add_last_activity_column())