import aiosqlite

async def adjust_weekly_limit_table(db_path):
    async with aiosqlite.connect(db_path) as db:
        # Create a new table without the UNIQUE constraint on 'TeamName'
        await db.execute("""
            CREATE TABLE IF NOT EXISTS new_weekly_limits (
                ID INTEGER PRIMARY KEY,
                TeamID INTEGER,
                TeamName TEXT NOT NULL,
                WeekStartDate DATETIME NOT NULL,
                MatchesPlayed INTEGER DEFAULT 0,
                PointsEarned INTEGER DEFAULT 0,
                FOREIGN KEY (TeamID) REFERENCES teams (TeamID)
            );
        """)

        # Copy all data from the old table to the new table
        await db.execute("""
            INSERT INTO new_weekly_limits (ID, TeamID, TeamName, WeekStartDate, MatchesPlayed, PointsEarned)
            SELECT ID, TeamID, TeamName, WeekStartDate, MatchesPlayed, PointsEarned
            FROM weekly_limits;
        """)

        # Drop the old table
        await db.execute("DROP TABLE weekly_limits")

        # Rename the new table
        await db.execute("ALTER TABLE new_weekly_limits RENAME TO weekly_limits")

        # Commit changes
        await db.commit()

# Path to your database
db_path = 'drafts.db'

# Run the function in an asyncio event loop
import asyncio
asyncio.run(adjust_weekly_limit_table(db_path))
