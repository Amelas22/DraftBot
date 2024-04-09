from sqlalchemy import Column, Integer, String, JSON
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base
from sqlalchemy.future import select
from sqlalchemy.orm import sessionmaker
import asyncio

# Your database setup
DATABASE_URL = "sqlite+aiosqlite:///drafts.db"
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
Base = declarative_base()

# Initialize DB (ensure this is called somewhere if needed)
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

class TeamRegistration(Base):
    __tablename__ = 'team_registration'
    ID = Column(Integer, primary_key=True)
    TeamID = Column(Integer)
    TeamName = Column(String)
    TeamMembers = Column(JSON)

async def update_team(session, team_registration_id, new_team_id=None, new_team_name=None, delete=False):
    async with session.begin():
        team_registration_stmt = select(TeamRegistration).where(TeamRegistration.ID == team_registration_id)
        team_registration_result = await session.execute(team_registration_stmt)
        team_registration = team_registration_result.scalars().first()
        
        if team_registration:
            if delete:
                await session.delete(team_registration)
                print(f"Team Registered for ID {team_registration_id} removed.")
                await session.commit()
            else:
                if new_team_id is not None:
                    team_registration.TeamID = new_team_id
                if new_team_name is not None:
                    team_registration.TeamName = new_team_name
                print(f"TeamRegistration ID {team_registration_id} updated: TeamID to {new_team_id}, TeamName to '{new_team_name}'.")
                await session.commit()
        else:
            print(f"TeamRegistration with ID {team_registration_id} not found.")

async def main():
    async with AsyncSessionLocal() as session:
        #await update_team(session, 37, delete=True)
        await update_team(session, 23, new_team_id=43, new_team_name="The Clean Sweep")
        

if __name__ == "__main__":
    asyncio.run(main())