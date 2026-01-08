"""Model for storing debt summary message references per guild."""
from sqlalchemy import Column, Integer, String, DateTime
from datetime import datetime
from database.models_base import Base


class DebtSummaryMessage(Base):
    """Stores the message reference for a guild's public debt summary."""
    __tablename__ = 'debt_summary_messages'

    id = Column(Integer, primary_key=True)
    guild_id = Column(String(64), nullable=False, unique=True)
    channel_id = Column(String(64), nullable=False)
    message_id = Column(String(64), nullable=False)
    last_updated = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    def __repr__(self):
        return f"<DebtSummaryMessage(guild_id={self.guild_id}, channel_id={self.channel_id})>"
