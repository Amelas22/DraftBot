import asyncio
import pandas as pd
from sqlalchemy import Column, Integer, String, DateTime, create_engine, MetaData, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from datetime import datetime

DATABASE_URL = "sqlite+aiosqlite:///drafts.db"
CSV_FILE_PATH = "completely_overwritten_database.csv"  # Path to your CSV file

engine = create_async_engine(DATABASE_URL, echo=False)
metadata = MetaData()

AsyncSessionLocal = sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession
)

Base = declarative_base()

class TempPlayerLimit(Base):
    __tablename__ = 'temp_player_limits'

    player_id = Column(String(64), primary_key=True)
    display_name = Column(String(128))  
    drafts_participated = Column(Integer, default=0)
    WeekStartDate = Column(DateTime, nullable=False)
    match_one_points = Column(Integer, default=0)
    match_two_points = Column(Integer, default=0)
    match_three_points = Column(Integer, default=0)
    match_four_points = Column(Integer, default=0)

async def create_temp_table():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def insert_data_to_temp_table():
    # Read the CSV file into a DataFrame
    df = pd.read_csv(CSV_FILE_PATH)

    # Fixed WeekStartDate for all entries
    fixed_week_start_date = datetime.strptime('2024-05-20 00:00:00.000000', '%Y-%m-%d %H:%M:%S.%f')

    async with AsyncSessionLocal() as session:
        async with session.begin():
            for index, row in df.iterrows():
                player_limit = TempPlayerLimit(
                    player_id=row['player_id'],
                    display_name=row['display_name'],
                    drafts_participated=row['drafts_participated'],
                    WeekStartDate=fixed_week_start_date,
                    match_one_points=row['match_one_points'],
                    match_two_points=row['match_two_points'],
                    match_three_points=row['match_three_points'],
                    match_four_points=row['match_four_points']
                )
                session.add(player_limit)
            await session.commit()

async def replace_old_table():
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS player_limits"))
        await conn.execute(text("ALTER TABLE temp_player_limits RENAME TO player_limits"))


async def main():
    await create_temp_table()
    await insert_data_to_temp_table()
    await replace_old_table()

if __name__ == "__main__":
    asyncio.run(main())
