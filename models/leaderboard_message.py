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
    draft_record_view_message_id = Column(String(64))
    match_win_view_message_id = Column(String(64))
    drafts_played_view_message_id = Column(String(64))
    time_vault_and_key_view_message_id = Column(String(64))
    draft_record_timeframe = Column(String(20))
    match_win_timeframe = Column(String(20))
    drafts_played_timeframe = Column(String(20))
    time_vault_and_key_timeframe = Column(String(20))

    def __repr__(self):
        return f"<LeaderboardMessage(guild_id={self.guild_id}, message_id={self.message_id})>"