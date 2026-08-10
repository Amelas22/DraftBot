from sqlalchemy import Column, String, DateTime, ForeignKey, text
from database.models_base import Base
from database.db_session import db_session
from datetime import datetime
import uuid


class SignUpHistory(Base):
    """Tracks when users join or leave draft sessions.

    Roster membership events are ROSTER_ACTIONS: 'join'/'leave' recorded
    live, plus 'synthetic_join' rows the dispnamefill0 migration
    reconstructed from final-roster sign_ups JSON for sessions that
    predate live recording (distinguishable provenance: a synthetic_join
    is roster membership, NOT an observed queue join). Ready-check events
    ('ready', 'not_ready', 'ready_timeout') are a separate subsystem and
    never indicate roster membership.
    """
    __tablename__ = 'sign_up_history'

    # Actions that assert roster membership. Any reader counting who was
    # IN a draft must filter to these; the migration keeps a frozen copy
    # of this set per the frozen-logic convention.
    ROSTER_ACTIONS = frozenset({"join", "leave", "synthetic_join"})

    id = Column(String(128), primary_key=True)  # Composite of session_id, user_id, and timestamp
    session_id = Column(String(64), ForeignKey('draft_sessions.session_id', ondelete='CASCADE'), nullable=False)
    user_id = Column(String(64), nullable=False)
    user_display_name = Column(String(128))
    action = Column(String(32), nullable=False)  # ROSTER_ACTIONS | ready | not_ready | ready_timeout
    timestamp = Column(DateTime, default=datetime.now, server_default=text('CURRENT_TIMESTAMP'), nullable=False)
    guild_id = Column(String(64), nullable=False)
    
    def __repr__(self) -> str:
        return (
            f"<SignUpHistory(session_id={self.session_id}, user_id={self.user_id}, "
            f"action={self.action}, timestamp={self.timestamp})>"
        )
    
    @classmethod
    async def _record_event(cls, session_id: str, user_id: str, display_name: str, action: str, guild_id: str):
        """Persist a single history row. Shared by the signup and ready-check recorders."""
        record = cls(
            id=str(uuid.uuid4()),
            session_id=session_id,
            user_id=user_id,
            user_display_name=display_name,
            action=action,
            timestamp=datetime.now(),
            guild_id=guild_id,
        )

        async with db_session() as session:
            session.add(record)
            await session.commit()

    @classmethod
    async def record_signup_event(cls, session_id: str, user_id: str, display_name: str, action: str, guild_id: str):
        """Record a signup event (action: 'join' or 'leave') for a draft session."""
        await cls._record_event(session_id, user_id, display_name, action, guild_id)

    @classmethod
    async def record_ready_event(cls, session_id: str, user_id: str, display_name: str, action: str, guild_id: str):
        """Record a ready-check response event (action: 'ready', 'not_ready', 'ready_timeout')."""
        await cls._record_event(session_id, user_id, display_name, action, guild_id)