
from sqlalchemy import Column, Integer, String, DateTime, JSON, Boolean, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, relationship, declarative_base
from datetime import datetime
import asyncio

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

class DraftSession(Base):
    __tablename__ = 'draft_sessions'

    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), nullable=False, unique=True)
    message_id = Column(String(64))
    draft_channel_id = Column(String(64))
    true_skill_draft = Column(Boolean, default=False)
    ready_check_message_id = Column(String(64))
    draft_link = Column(String(256))
    ready_check_status = Column(JSON)
    draft_start_time = Column(DateTime, default=datetime)
    deletion_time = Column(DateTime)
    teams_start_time = Column(DateTime)
    draft_chat_channel = Column(String(64))
    guild_id = Column(String(64))
    draft_id = Column(String(64))
    trophy_drafters = Column(JSON)
    team_a = Column(JSON)
    team_b = Column(JSON)
    victory_message_id_draft_chat = Column(String(64))
    victory_message_id_results_channel = Column(String(64))
    winning_gap = Column(Integer)
    draft_summary_message_id = Column(String(64))
    matches = Column(JSON)
    match_counter = Column(Integer, default=1)
    sign_ups = Column(JSON)
    channel_ids = Column(JSON)
    session_type = Column(String(64))
    session_stage = Column(String(64))
    team_a_name = Column(String(128))
    team_b_name = Column(String(128))
    are_rooms_processing = Column(Boolean, default=False)
    premade_match_id = Column(String(128))
    tracked_draft = Column(Boolean, default=False)
    seating_order = Column(String(128))
    match_results = relationship("MatchResult", back_populates="draft_session", foreign_keys="[MatchResult.session_id]")
    def __repr__(self):
        return f"<DraftSession(session_id={self.session_id}, guild_id={self.guild_id})>"
    
async def add_seating_order_column():
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE draft_sessions ADD COLUMN seating_order TEXT"))

async def main():
    # Call the function to add the seating_order column
    await add_seating_order_column()

if __name__ == "__main__":
    asyncio.run(main())