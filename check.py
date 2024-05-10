import aiosqlite
import asyncio

async def check_table_schema(db_path):
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("PRAGMA table_info(draft_session);")
        columns = await cursor.fetchall()
        for col in columns:
            print(col)

db_path = 'drafts.db'
asyncio.run(check_table_schema(db_path))
