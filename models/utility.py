from sqlalchemy import Column, Integer, String
from database.models_base import Base

class TeamFinder(Base):
    __tablename__ = 'team_finder'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(64), nullable=False)
    display_name = Column(String(128), nullable=False)
    timezone = Column(String(64), nullable=False)
    message_id = Column(String(64))
    channel_id = Column(String(64))
    guild_id = Column(String(64))
