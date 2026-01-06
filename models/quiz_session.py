from sqlalchemy import Column, Integer, String, DateTime, text, JSON, Index
from database.models_base import Base

class QuizSession(Base):
    __tablename__ = 'quiz_sessions'

    # Primary key
    quiz_id = Column(String(64), primary_key=True)  # Format: guild_id-timestamp

    # Human-friendly sequential ID per guild
    display_id = Column(Integer, nullable=False)

    # Location
    guild_id = Column(String(64), nullable=False)
    channel_id = Column(String(64), nullable=False)
    message_id = Column(String(64), nullable=True)  # Public message ID

    # Draft reference
    draft_session_id = Column(String(128), nullable=False)  # Foreign key to draft_sessions
    starting_seat = Column(Integer, nullable=True)  # Starting seat position for pack trace (0-indexed)

    # Quiz data (JSON serialized)
    pack_trace_data = Column(JSON, nullable=False)  # Serialized PackTrace
    correct_answers = Column(JSON, nullable=False)  # List of 4 card IDs

    # Metadata
    posted_by = Column(String(64), nullable=False)  # Mod who posted it
    posted_at = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'))

    # Stats
    total_participants = Column(Integer, default=0, server_default=text('0'))

    # Table constraints
    __table_args__ = (
        Index('ix_quiz_sessions_guild_display_id', 'guild_id', 'display_id', unique=True),
    )

    def __repr__(self):
        return f"<QuizSession(quiz_id={self.quiz_id}, display_id=#{self.display_id}, guild_id={self.guild_id})>"
