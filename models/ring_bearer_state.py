from sqlalchemy import Column, String, DateTime, Index
from database.models_base import Base


class RingBearerState(Base):
    """
    Tracks the current ring bearer for each guild.

    The ring bearer is a special single-holder role that can be claimed by:
    1. Becoming #1 on any win streak leaderboard
    2. Defeating the current ring bearer in a match
    3. Being #1 and extending your streak while not having the role

    Only one player per guild can hold the ring bearer role at a time.
    """
    __tablename__ = 'ring_bearer_state'

    guild_id = Column(String(64), primary_key=True)
    current_bearer_id = Column(String(64), nullable=True)  # Current ring bearer's Discord ID
    acquired_at = Column(DateTime, nullable=True)  # When they acquired the role
    acquired_via = Column(String(32), nullable=True)  # How: 'match_defeat', 'win_streak', 'perfect_streak', 'draft_win_streak'
    previous_bearer_id = Column(String(64), nullable=True)  # Who they took it from (for announcements)

    __table_args__ = (
        Index('idx_ring_bearer_guild', 'guild_id'),
    )

    @classmethod
    async def get_ring_bearer(cls, guild_id: str, session):
        """Get the current ring bearer state for a guild."""
        from sqlalchemy import select
        result = await session.execute(
            select(cls).where(cls.guild_id == guild_id)
        )
        return result.scalar_one_or_none()

    @classmethod
    async def set_ring_bearer(cls, guild_id: str, bearer_id: str, acquired_via: str,
                             previous_bearer_id, session):
        """Set the ring bearer for a guild (creates or updates)."""
        from datetime import datetime
        from sqlalchemy import select

        result = await session.execute(
            select(cls).where(cls.guild_id == guild_id)
        )
        ring_bearer_state = result.scalar_one_or_none()

        if ring_bearer_state:
            ring_bearer_state.current_bearer_id = bearer_id
            ring_bearer_state.acquired_at = datetime.now()
            ring_bearer_state.acquired_via = acquired_via
            ring_bearer_state.previous_bearer_id = previous_bearer_id
        else:
            ring_bearer_state = cls(
                guild_id=guild_id,
                current_bearer_id=bearer_id,
                acquired_at=datetime.now(),
                acquired_via=acquired_via,
                previous_bearer_id=previous_bearer_id
            )
            session.add(ring_bearer_state)

        await session.commit()
        return ring_bearer_state
