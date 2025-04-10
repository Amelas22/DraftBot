from sqlalchemy import Column, Integer, String, DateTime
from database.models_base import Base
from datetime import datetime

class LeaderboardMessage(Base):
    __tablename__ = 'leaderboard_messages'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String(64), nullable=False)
    channel_id = Column(String(64), nullable=False)
    message_id = Column(String(64), nullable=False)
    last_updated = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    def __repr__(self):
        return f"<LeaderboardMessage(guild_id={self.guild_id}, message_id={self.message_id})>"