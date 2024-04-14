from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, inspect, func, text
import pandas as pd
import asyncio
import os

# Your existing database and table setup
DATABASE_URL = "sqlite+aiosqlite:///drafts.db" 

engine = create_async_engine(DATABASE_URL, echo=False)

AsyncSessionLocal = sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession
)

Base = declarative_base()


# Define the Team class as per your existing setup and add the new column
class Team(Base):
    __tablename__ = 'teams'
    TeamID = Column(Integer, primary_key=True)
    TeamName = Column(String(128), unique=True, nullable=False)
    MatchesCompleted = Column(Integer, default=0)
    MatchWins = Column(Integer, default=0)
    PointsEarned = Column(Integer, default=0)
    PreseasonPoints = Column(Integer, default=0)  # New column

# Function to add the new column
async def add_preseason_points_column():
    async with engine.begin() as conn:
        # Check if the 'PreseasonPoints' column already exists
        columns = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_columns(Team.__tablename__))
        column_names = [col['name'] for col in columns]
        if 'PreseasonPoints' not in column_names:
            await conn.execute(text("ALTER TABLE teams ADD COLUMN PreseasonPoints INTEGER DEFAULT 0"))
            print("PreseasonPoints column added.")
        else:
            print("PreseasonPoints column already exists.")

# Function to update the PreseasonPoints based on the given data
async def update_preseason_points(data):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            for team_name, preseason_points in data.items():
                # Assuming team names are unique and case-insensitive in the database
                team_name = team_name.strip().lower()
                stmt = update(Team).where(func.lower(Team.TeamName) == team_name).values(PreseasonPoints=preseason_points)
                await session.execute(stmt)
        await session.commit()

# Main function to perform the entire operation
async def main():
    # Read the provided data (assuming you have a CSV file)
    # Replace '/path/to/your/csvfile.csv' with the actual file path
    data = pd.read_csv('preseasonpoints.csv').set_index('TeamName')['PreseasonPoints'].to_dict()

    # Add the new column
    await add_preseason_points_column()
    
    # Update the preseason points
    await update_preseason_points(data)

# Run the main function
asyncio.run(main())
