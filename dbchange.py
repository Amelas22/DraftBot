import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.sql import text

DATABASE_URL = "sqlite+aiosqlite:///drafts.db"
engine = create_async_engine(DATABASE_URL, echo=True)

async def add_column_if_not_exists():
    async with engine.connect() as conn:
        # Check if the column already exists
        exists_query = """
        PRAGMA table_info(player_limits);
        """
        result = await conn.execute(text(exists_query))
        columns = result.fetchall()  # Correct usage without 'await'
        column_names = [column[1] for column in columns]  # Column names are in the second position
        
        if "match_four_points" not in column_names:
            # Add the swiss_matches column to the player_limits table
            add_column_query = """
            ALTER TABLE player_limits ADD COLUMN match_four_points INTEGER;
            """
            await conn.execute(text(add_column_query))
            print("Column 'match_four_points' added to 'player_limits' table.")
        else:
            print("Column 'match_four_points' already exists in 'player_limits' table.")

async def main():
    await add_column_if_not_exists()

if __name__ == "__main__":
    asyncio.run(main())
