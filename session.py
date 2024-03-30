import discord
from sqlalchemy import Column, Integer, String, DateTime, JSON, select, Boolean, ForeignKey, desc
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime

DATABASE_URL = "sqlite+aiosqlite:///drafts.db" 

engine = create_async_engine(DATABASE_URL, echo=True)

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
    match_counter = Column(Integer, default=1)
    sign_ups = Column(JSON)
    channel_ids = Column(JSON)
    session_type = Column(String(64))
    session_stage = Column(String(64))
    team_a_name = Column(String(128))
    team_b_name = Column(String(128))
    are_rooms_processing = Column(Boolean, default=False)
    match_results = relationship("MatchResult", back_populates="draft_session")
    def __repr__(self):
        return f"<DraftSession(session_id={self.session_id}, guild_id={self.guild_id})>"

class MatchResult(Base):
    __tablename__ = 'match_results'

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey('draft_sessions.id'))
    match_number = Column(Integer)
    player1_id = Column(String(64))
    player1_wins = Column(Integer, default=0)
    player2_id = Column(String(64))
    player2_wins = Column(Integer, default=0)
    winner_id = Column(String(64), nullable=True)
    draft_session = relationship("DraftSession", back_populates="match_results")

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
            # Order the DraftSessions by their id in descending order to get the most recent ones
            stmt = select(DraftSession).order_by(desc(DraftSession.id)).limit(10)
            result = await session.execute(stmt)
            draft_sessions = result.scalars().all()

    for draft_session in draft_sessions:
        if draft_session.draft_channel_id and draft_session.message_id:
            channel_id = int(draft_session.draft_channel_id)
            channel = bot.get_channel(channel_id)
            if channel:
                try:
                    message = await channel.fetch_message(int(draft_session.message_id))
                    from views import PersistentView
                    view = PersistentView(bot=bot,
                                          draft_session_id=draft_session.session_id,
                                          session_type=draft_session.session_type,
                                          team_a_name=draft_session.team_a_name,
                                          team_b_name=draft_session.team_b_name)
                    await message.edit(view=view)  # Reattach the view
                except discord.NotFound:
                    # Handle cases where the message or channel might have been deleted
                    print(f"Message or channel not found for session: {draft_session.session_id}")
                except Exception as e:
                    # Log or handle any other exceptions
                    print(f"Failed to re-register view for session: {draft_session.session_id}, error: {e}")
        else:
            # Log or handle sessions without a valid channel or message ID
            print(f"Session {draft_session.session_id} does not have a valid channel and/or message ID.")
