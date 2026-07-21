from sqlalchemy import Column, Integer, String, DateTime, text, JSON, Index
from database.models_base import Base


class TrophyQuizSession(Base):
    __tablename__ = 'trophy_quiz_sessions'

    quiz_id = Column(String(64), primary_key=True)   # guild_id-timestamp
    display_id = Column(Integer, nullable=False)
    guild_id = Column(String(64), nullable=False)
    channel_id = Column(String(64), nullable=False)
    message_id = Column(String(64), nullable=True)
    draft_session_id = Column(String(128), nullable=False)
    decks = Column(JSON, nullable=False)             # [{slot, drafter_id, wins, pool}] x2
    posted_by = Column(String(64), nullable=False)
    posted_at = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'))
    total_participants = Column(Integer, default=0, server_default=text('0'))

    __table_args__ = (
        Index('ix_trophy_quiz_sessions_guild_display_id', 'guild_id', 'display_id', unique=True),
    )
