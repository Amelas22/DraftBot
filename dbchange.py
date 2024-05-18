import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.sql import text

DATABASE_URL = "sqlite+aiosqlite:///drafts.db"
engine = create_async_engine(DATABASE_URL, echo=True)

async def add_column_if_not_exists():
    async with engine.connect() as conn:
        # Check if the column already exists
        exists_query = """
        PRAGMA table_info(draft_sessions);
        """
        result = await conn.execute(text(exists_query))
        columns = result.fetchall()  # Correct usage without 'await'
        column_names = [column[1] for column in columns]  # Column names are in the second position
        
        if "cube" not in column_names:
            # Add the swiss_matches column to the draft_sessions table
            add_column_query = """
            ALTER TABLE draft_sessions ADD COLUMN cube VARCHAR(128);
            """
            await conn.execute(text(add_column_query))
            print("Column 'cube' added to 'draft_sessions' table.")
        else:
            print("Column 'cube' already exists in 'draft_sessions' table.")
        
        if "data_received" not in column_names:
            # Add the swiss_matches column to the draft_sessions table
            add_column_query = """
            ALTER TABLE draft_sessions ADD COLUMN data_received BOOLEAN;
            """
            await conn.execute(text(add_column_query))
            print("Column 'data_received' added to 'draft_sessions' table.")
        else:
            print("Column 'data_received' already exists in 'draft_sessions' table.")

async def main():
    await add_column_if_not_exists()

if __name__ == "__main__":
    asyncio.run(main())
