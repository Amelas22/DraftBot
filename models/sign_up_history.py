from sqlalchemy import Column, String, DateTime, ForeignKey, text
from database.models_base import Base
from datetime import datetime


class SignUpHistory(Base):
    """Tracks when users join or leave draft sessions."""
    __tablename__ = 'sign_up_history'
    
    id = Column(String(128), primary_key=True)  # Composite of session_id, user_id, and timestamp
    session_id = Column(String(64), ForeignKey('draft_sessions.session_id', ondelete='CASCADE'), nullable=False)
    user_id = Column(String(64), nullable=False)
    user_display_name = Column(String(128))
    action = Column(String(16), nullable=False)  # 'join' or 'leave'
    timestamp = Column(DateTime, default=datetime.now, server_default=text('CURRENT_TIMESTAMP'), nullable=False)
    guild_id = Column(String(64), nullable=False)
    
    def __repr__(self) -> str:
        return (
            f"<SignUpHistory(session_id={self.session_id}, user_id={self.user_id}, "
            f"action={self.action}, timestamp={self.timestamp})>"
        )