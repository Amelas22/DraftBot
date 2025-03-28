from sqlalchemy import Column, Integer, String, DateTime, Float, ForeignKey
from database.models_base import Base

class PlayerStats(Base):
    __tablename__ = 'player_stats'
    
    player_id = Column(String(64), primary_key=True)
    guild_id = Column(String(64), primary_key=True)
    display_name = Column(String(128))  
    drafts_participated = Column(Integer, default=0)
    games_won = Column(Integer, default=0)
    games_lost = Column(Integer, default=0)
    elo_rating = Column(Float, default=1200)
    true_skill_mu = Column(Float, default=25)
    true_skill_sigma = Column(Float, default=8.333)

    def __repr__(self):
        return f"<PlayerStats(player_id={self.player_id}, display_name={self.display_name})>"

class PlayerLimit(Base):
    __tablename__ = 'player_limits'

    player_id = Column(String(64), primary_key=True)
    display_name = Column(String(128))  
    drafts_participated = Column(Integer, default=0)
    WeekStartDate = Column(DateTime, nullable=False, primary_key=True)
    match_one_points = Column(Integer, default=0)
    match_two_points = Column(Integer, default=0)
    match_three_points = Column(Integer, default=0)
    match_four_points = Column(Integer, default=0) 