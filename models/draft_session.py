from sqlalchemy import Column, Integer, String, DateTime, JSON, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy import select
from datetime import datetime

from database.models_base import Base
from database.db_session import db_session

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
    draft_start_time = Column(DateTime, default=datetime.now)
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
    swiss_matches = Column(JSON)
    draft_data = Column(JSON)
    data_received = Column(Boolean, default=False)
    cube = Column(String(128))
    live_draft_message_id = Column(String(64))
    min_stake = Column(Integer, default=10)
    logs_channel_id = Column(String(64))
    logs_message_id = Column(String(64))
    magicprotools_links = Column(JSON)
    
    # Relationships
    match_results = relationship("MatchResult", back_populates="draft_session", 
                                foreign_keys="[MatchResult.session_id]")
    stakes = relationship("StakeInfo", backref="draft_session")
    
    def __repr__(self):
        return f"<DraftSession(session_id={self.session_id}, guild_id={self.guild_id})>"

    @classmethod
    async def get_by_session_id(cls, session_id: str):
        """Get a draft session by its session ID"""
        async with db_session() as session:
            query = select(cls).filter_by(session_id=session_id)
            result = await session.execute(query)
            return result.scalar_one_or_none()
    
    @classmethod
    async def get_by_channel_id(cls, channel_id: str):
        """Get a draft session associated with a specific channel"""
        async with db_session() as session:
            query = select(cls).filter_by(draft_chat_channel=channel_id)
            result = await session.execute(query)
            return result.scalar_one_or_none()
    
    def is_user_participating(self, user_id: str) -> bool:
        """Check if a user is participating in this draft session"""
        return user_id in self.team_a or user_id in self.team_b
    
    @classmethod
    async def create_session(cls, **kwargs):
        """Create a new draft session with the given attributes"""
        session_obj = cls(**kwargs)
        async with db_session() as session:
            session.add(session_obj)
            await session.flush()  # Flush to get the ID without committing
            return session_obj
    
    async def update(self, **kwargs):
        """Update this draft session with the given attributes"""
        async with db_session() as session:
            # Refresh the object from the database
            await session.refresh(self)
            
            # Update attributes
            for key, value in kwargs.items():
                if hasattr(self, key):
                    setattr(self, key, value)
            
            session.add(self)
    
    @classmethod
    async def get_active_sessions(cls, guild_id: str = None):
        """Get all active draft sessions, optionally filtered by guild ID"""
        async with db_session() as session:
            query = select(cls).where(cls.session_stage != "COMPLETED")
            
            if guild_id:
                query = query.filter_by(guild_id=guild_id)
            
            result = await session.execute(query)
            return result.scalars().all()
    
    @classmethod
    async def get_by_draft_id(cls, draft_id: str):
        """Get a draft session by its draft ID"""
        async with db_session() as session:
            query = select(cls).filter_by(draft_id=draft_id)
            result = await session.execute(query)
            return result.scalar_one_or_none()
