import asyncio
from sqlalchemy import text
from session import engine

async def add_live_draft_message_id_column():
    async with engine.begin() as conn:
        try:
            # Check if the column already exists
            check_query = "PRAGMA table_info(draft_sessions)"
            result = await conn.execute(text(check_query))
            columns = result.fetchall()
            
            column_exists = any(column[1] == 'live_draft_message_id' for column in columns)
            
            if not column_exists:
                # Add the column if it doesn't exist
                add_column_query = "ALTER TABLE draft_sessions ADD COLUMN live_draft_message_id VARCHAR(64)"
                await conn.execute(text(add_column_query))
                print("Successfully added live_draft_message_id column to draft_sessions table")
            else:
                print("Column live_draft_message_id already exists in draft_sessions table")
                
        except Exception as e:
            print(f"Error adding column: {e}")

# Run the migration
if __name__ == "__main__":
    asyncio.run(add_live_draft_message_id_column())