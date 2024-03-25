import discord
from sqlalchemy import Column, Integer, String, DateTime, JSON, select, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from datetime import datetime

DATABASE_URL = "sqlite+aiosqlite:///drafts.db"  # Use aiosqlite for async operations with SQLite

engine = create_async_engine(DATABASE_URL, echo=True)

# AsyncSession class
AsyncSessionLocal = sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession
)

async def init_db():
    async with engine.begin() as conn:
        # This is the correct place for initializing your tables
        await conn.run_sync(Base.metadata.create_all)

Base = declarative_base()

class DraftSession(Base):
    __tablename__ = 'draft_sessions'

    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), nullable=False, unique=True)
    message_id = Column(String(64))
    draft_channel_id = Column(String(64))
    draft_message_id = Column(String(64))
    ready_check_message_id = Column(String(64))
    draft_link = Column(String(256))
    ready_check_status = Column(JSON)
    draft_start_time = Column(DateTime, default=datetime)
    deletion_time = Column(DateTime)
    teams_start_time = Column(DateTime)
    draft_chat_channel = Column(String(64))
    guild_id = Column(String(64))
    draft_id = Column(String(64))
    pairings = Column(JSON)
    team_a = Column(JSON)
    team_b = Column(JSON)
    victory_message_id = Column(String(64))
    draft_summary_message_id = Column(String(64))
    matches = Column(JSON)
    match_results = Column(JSON)
    match_counter = Column(Integer, default=1)
    sign_ups = Column(JSON)
    channel_ids = Column(JSON)
    session_type = Column(String(64))
    session_stage = Column(String(64))
    team_a_name = Column(String(128))
    team_b_name = Column(String(128))
    are_rooms_processing = Column(Boolean, default=False)

    def __repr__(self):
        return f"<DraftSession(session_id={self.session_id}, guild_id={self.guild_id})>"

async def get_draft_session(session_id: str):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            query = select(DraftSession).filter_by(session_id=session_id)
            result = await session.execute(query)
            draft_session = result.scalars().first()
            return draft_session
        
async def re_register_views(bot):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Fetch all sessions or a subset that you know will have active views
            result = await session.execute(select(DraftSession))
            draft_sessions = result.scalars().all()

    for draft_session in draft_sessions:
        if draft_session.draft_channel_id:
            # Only proceed if the draft_channel_id is not None and is a valid ID
            channel_id = int(draft_session.draft_channel_id)
            channel = bot.get_channel(channel_id)
            if channel:
                try:
                    message = await channel.fetch_message(draft_session.message_id)
                    from views import PersistentView
                    view = PersistentView(draft_session)  # Recreate the PersistentView for this session
                    await message.edit(view=view)  # Reattach the view
                except discord.NotFound:
                    # Handle cases where the message or channel might have been deleted
                    print(f"Message or channel not found for session: {draft_session.session_id}")
                except Exception as e:
                    print(f"Failed to re-register view for session: {draft_session.session_id}, error: {e}")
        else:
            print(f"Session {draft_session.session_id} does not have a valid channel ID.")
