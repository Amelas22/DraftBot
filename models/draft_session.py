from sqlalchemy import Column, Integer, String, DateTime, JSON, Boolean, text, Index
from sqlalchemy.orm import relationship
from sqlalchemy import select, desc
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
    lobby_ready_check_message_id = Column(String(64))  # lobby ReadyCheckView message; stripped on restart
    draft_link = Column(String(256))
    ready_check_status = Column(JSON) # deprecated
    draft_start_time = Column(DateTime, default=datetime.now)
    deletion_time = Column(DateTime)
    teams_start_time = Column(DateTime)
    draft_chat_channel = Column(String(64))
    guild_id = Column(String(64))
    draft_id = Column(String(64))
    friendly_id = Column(String(32))
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
    # Set only once every room for this draft exists and the pairings work has been
    # committed -- i.e. written AFTER the work, not before it. The old completeness
    # check read draft_chat_channel, which create_team_channel commits in its own
    # session while creating the FIRST of three channels: any failure after that
    # left a flag saying "done" over a half-created draft, and every retry believed
    # it. This column is the marker that a run actually finished.
    rooms_created_at = Column(DateTime, nullable=True)
    premade_match_id = Column(String(128))
    tournament_match_id = Column(Integer, nullable=True)  # links result auto-recording to a TournamentMatch
    tracked_draft = Column(Boolean, default=False)
    swiss_matches = Column(JSON)
    draft_data = Column(JSON)
    data_received = Column(Boolean, default=False)
    logs_captured_at = Column(DateTime)  # set when the log is captured to DB/Spaces (pre-publish)
    spaces_object_key = Column(String(256), nullable=True)  # DigitalOcean Spaces object path
    unlock_at = Column(DateTime)              # when the public embed may publish (logs_captured_at + PUBLISH_DELAY; manual release = now)
    team_logs_posted_at = Column(DateTime)    # when per-team pools were posted to team channels (immediate; no time gate)
    # Where each team's drafted pools are being delivered: the pools thread,
    # or the team channel itself when Discord refused a thread. Whichever one
    # carried a pool is recorded here, and it IS the record of who has posted
    # -- so a retry resumes into the same place rather than re-posting, and a
    # team the fallback already served never gets a second, empty thread.
    team_a_pools_destination_id = Column(String(64), nullable=True)
    team_b_pools_destination_id = Column(String(64), nullable=True)
    # Same idea for the OPEN pools thread a tournament match gets in the shared draft
    # chat. Separate from the two above because it is a third destination with its own
    # partial-failure story: the open thread can fail while both team threads succeed.
    open_pools_destination_id = Column(String(64), nullable=True)
    cube = Column(String(128))
    live_draft_message_id = Column(String(64))
    min_stake = Column(Integer, default=10, server_default=text('10'))
    # A premade draft's fixed entry fee: everyone on either team pays exactly
    # this to join. NULL means a free draft, which is every premade that
    # predates the option. Distinct from min_stake, which is a FLOOR players
    # bet above -- here there is nothing to choose.
    entry_fee = Column(Integer, nullable=True)
    packs_per_player = Column(Integer, default=3, server_default=text('3'))
    cards_per_pack = Column(Integer, default=15, server_default=text('15'))
    logs_channel_id = Column(String(64))
    logs_message_id = Column(String(64))
    magicprotools_links = Column(JSON)
    should_ping = Column(Boolean, default=False)
    pack_first_picks = Column(JSON)
    draftmancer_role_users = Column(JSON)
    status_message_id = Column(String, nullable=True)

    __table_args__ = (
        # Enforces the invariant a race between two open pickers could otherwise
        # break: at most one draft session per tournament match. SQLite permits
        # multiple NULLs in a unique index, so unlinked (non-tournament) drafts
        # are unaffected.
        Index('ix_draft_sessions_tournament_match_id', 'tournament_match_id', unique=True),
    )

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

    @classmethod
    async def get_by_any_channel_id(cls, channel_id: int | str):
        """Get a draft session whose main chat OR any created channel matches.

        Unlike get_by_channel_id (draft_chat_channel only), this also searches
        the channel_ids JSON so lookups work from team chat channels too.

        The SQL LIKE prefilter narrows candidates to sessions whose channel_ids
        JSON contains the channel ID as a substring; the Python
        channel_ids_contains check then confirms exact membership to avoid
        false positives (e.g. channel 123 matching stored ID 51234).
        """
        from helpers.substitutes import channel_ids_contains

        channel_id = str(channel_id)
        found = await cls.get_by_channel_id(channel_id)
        if found:
            return found
        async with db_session() as session:
            query = (select(cls)
                     .where(cls.channel_ids.isnot(None))
                     .where(cls.channel_ids.cast(String).like(f'%{channel_id}%'))
                     .order_by(desc(cls.draft_start_time)))
            result = await session.execute(query)
            for draft in result.scalars().all():
                if channel_ids_contains(draft.channel_ids, channel_id):
                    return draft
        return None

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

    @classmethod
    async def get_by_friendly_id(cls, guild_id: str, friendly_id: str):
        """Get a draft session by its friendly id, scoped to a guild.

        friendly_id isn't enforced unique -- duplicates within a guild can
        happen, so this returns the most recently created match rather than
        raising on more than one result."""
        async with db_session() as session:
            query = (
                select(cls)
                .filter_by(guild_id=guild_id, friendly_id=friendly_id)
                .order_by(cls.id.desc())
            )
            result = await session.execute(query)
            return result.scalars().first()

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
