from sqlalchemy import Column, Integer, String, DateTime, JSON, Boolean, text
from sqlalchemy.orm import relationship
from sqlalchemy import select
from datetime import datetime
from urllib.parse import quote
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
    min_stake = Column(Integer, default=10, server_default=text('10'))
    logs_channel_id = Column(String(64))
    logs_message_id = Column(String(64))
    magicprotools_links = Column(JSON)
    should_ping = Column(Boolean, default=False)
    pack_first_picks = Column(JSON)
    draftmancer_role_users = Column(JSON)
    status_message_id = Column(String, nullable=True)
    
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
    async def get_active_draft_for_user(cls, channel_id: str, user_id: str):
        """
        Find the most recent active draft where:
        1. The channel matches draft_channel_id
        2. The user is in the sign_ups
        3. The draft is not completed
        
        Args:
            channel_id: The Discord channel ID
            user_id: The Discord user ID
            
        Returns:
            The most recent matching DraftSession or None
        """
        async with db_session() as session:
            from sqlalchemy import select, and_, desc
            
            # Create query to find matching drafts
            stmt = select(cls).where(
                and_(
                    cls.draft_channel_id == channel_id,
                    cls.session_stage.isnot(None)
                )
            ).order_by(desc(cls.draft_start_time))  # Most recent first
            
            result = await session.execute(stmt)
            draft_sessions = result.scalars().all()
            
            # Filter for drafts where user is in sign_ups
            for draft in draft_sessions:
                sign_ups = draft.sign_ups or {}
                if user_id in sign_ups:
                    return draft
                    
            return None
        
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
            # Merge object into this session (handles detached objects)
            self = session.merge(self)

            # Update attributes
            for key, value in kwargs.items():
                if hasattr(self, key):
                    setattr(self, key, value)

            await session.commit()
    
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

    def get_draft_link_for_user(self, user_name: str) -> str:
        """
        Get a personalized draft link for a specific user.
        
        Args:
            user_name (str): The username to add to the draft link
            
        Returns:
            str: The draft link with the username parameter added
        """
        if not self.draft_link:
            return None
        
        # URL-encode the username to handle spaces and special characters
        encoded_username = quote(user_name)
        
        # Handle case where draft_link might already have parameters
        separator = '&' if '?' in self.draft_link else '?'
        return f"{self.draft_link}{separator}userName={encoded_username}"
