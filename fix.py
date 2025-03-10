import asyncio
from sqlalchemy import Column, String, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from loguru import logger

# Import your database connection string
# This should match what's in your AsyncSessionLocal configuration
from session import DATABASE_URL

async def add_notification_message_id_column():
    """Add notification_message_id column to the messages table if it doesn't exist."""
    # Create engine and session
    engine = create_async_engine(DATABASE_URL)
    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with engine.begin() as conn:
        # Check if the column already exists
        try:
            result = await conn.execute(text("SELECT notification_message_id FROM messages LIMIT 1"))
            logger.info("Column 'notification_message_id' already exists.")
            return
        except Exception:
            logger.info("Column 'notification_message_id' does not exist. Adding it now.")
            
            # Add the column
            await conn.execute(
                text("ALTER TABLE messages ADD COLUMN notification_message_id VARCHAR(64)")
            )
            logger.info("Column 'notification_message_id' has been added successfully.")

if __name__ == "__main__":
    asyncio.run(add_notification_message_id_column())