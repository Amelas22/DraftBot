from sqlalchemy import Column, Integer, String, DateTime, Index
from database.models_base import Base


class WinStreakHistory(Base):
    __tablename__ = 'win_streak_history'

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(String(64), nullable=False)
    guild_id = Column(String(64), nullable=False)
    streak_length = Column(Integer, nullable=False)
    started_at = Column(DateTime, nullable=False)  # When streak began
    ended_at = Column(DateTime, nullable=True)     # When streak ended (NULL = active)

    # Indexes for fast timeframe queries
    __table_args__ = (
        Index('idx_streak_player_guild', 'player_id', 'guild_id'),
        Index('idx_streak_guild_started', 'guild_id', 'started_at'),
        Index('idx_streak_guild_ended', 'guild_id', 'ended_at'),
    )

    def __repr__(self):
        status = "active" if self.ended_at is None else "ended"
        return f"<WinStreakHistory({self.player_id}: {self.streak_length} wins, {status})>"
