from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy import text
from session import Base, DraftSession, MatchResult, PlayerStats, Team, Match, WeeklyLimit, Challenge

# Your database URL
DATABASE_URL = "sqlite+aiosqlite:///drafts.db" 

# Create the async engine
engine = create_async_engine(DATABASE_URL, echo=False)

# Configure sessionmaker to use with the async engine
AsyncSessionLocal = sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession
)

async def cleanup_database():
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Delete data from specified tables using text() to wrap raw SQL commands
            await session.execute(text('DELETE FROM swiss_challenges'))
            
        await session.commit()

if __name__ == '__main__':
    import asyncio
    asyncio.run(cleanup_database())