import pandas as pd
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, JSON, text

# Assuming your database setup is as described and these imports are correct
DATABASE_URL = "sqlite+aiosqlite:///drafts.db" 

engine = create_async_engine(DATABASE_URL, echo=False)

AsyncSessionLocal = sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession
)

async def init_db():

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

Base = declarative_base()

class TeamRegistration(Base):
    __tablename__ = 'team_registration'

    ID = Column(Integer, primary_key=True)
    TeamID = Column(Integer)
    TeamName = Column(String(128), unique=True, nullable=False)
    TeamMembers = Column(JSON)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def clear_team_registration():
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM team_registration"))  # Clear the table
        await session.commit()

# Function to read and prepare CSV data
def prepare_csv_data(filepath):
    data = pd.read_csv(filepath)
    prepared_data = []
    
    for _, row in data.iterrows():
        # Handle NaN values and ensure data types
        try:
            team_id = int(row['TeamID'])  # This will raise a ValueError if it's NaN or not an integer
            team_members = {
                str(int(row['Player1 UserID'])): row['Player 1'],
                str(int(row['Player2 UserId'])): row['Player 2'],
                str(int(row['Player3 UserID'])): row['Player 3']
            }
            prepared_data.append({
                "TeamID": team_id,
                "TeamName": str(row['Team Name']),  # Ensure TeamName is a string
                "TeamMembers": team_members
            })
        except ValueError as e:
            print(f"Skipping row due to error: {e}")
    return prepared_data


async def insert_single_team():
    async with AsyncSessionLocal() as session:
        new_team = TeamRegistration(
            TeamID=1, TeamName="Test Team", TeamMembers={"Player1": "Name1", "Player2": "Name2"})
        session.add(new_team)
        await session.commit()

# Function to insert data into the database
async def insert_team_data(prepared_data):
    async with AsyncSessionLocal() as session:
        for item in prepared_data:
            new_team = TeamRegistration(TeamID=item['TeamID'], TeamName=item['TeamName'], TeamMembers=item['TeamMembers'])
            session.add(new_team)
        await session.commit()

# Main async function to run the database initialization and data insertion
async def main():
    await init_db()
    #await insert_single_team()
    await clear_team_registration()  # Ensure the database and tables are initialized
    prepared_data = prepare_csv_data('TeamRegistration.csv')
    await insert_team_data(prepared_data)

# Run the main function
import asyncio
asyncio.run(main())
