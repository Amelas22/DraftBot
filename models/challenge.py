from sqlalchemy import Column, Integer, String, DateTime, JSON
from database.models_base import Base

class Challenge(Base):
    __tablename__ = 'challenges'
    
    id = Column(Integer, primary_key=True)
    initial_user = Column(String(64))
    opponent_user = Column(String(64))
    message_id = Column(String(64), nullable=True)
    channel_id = Column(String(64), nullable=True)
    guild_id = Column(String(64))
    team_a_id = Column(Integer, nullable=False)
    team_b_id = Column(Integer, nullable=True)
    start_time = Column(DateTime, nullable=False)
    team_a = Column(String(128))
    team_b = Column(String(128))
    cube = Column(String(128))

class SwissChallenge(Base):
    __tablename__ = 'swiss_challenges'
    
    id = Column(Integer, primary_key=True)
    initial_user = Column(String(64))
    sign_ups = Column(JSON)
    message_id = Column(String(64), nullable=True)
    channel_id = Column(String(64), nullable=True)
    guild_id = Column(String(64))
    start_time = Column(DateTime, nullable=False)
    cube = Column(String(128))
