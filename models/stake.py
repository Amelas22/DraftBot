from sqlalchemy import Column, Integer, String, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from database.models_base import Base

class StakeInfo(Base):
    __tablename__ = 'stake_info'
    
    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), ForeignKey('draft_sessions.session_id'))
    player_id = Column(String(64), nullable=False)
    max_stake = Column(Integer, nullable=False)
    assigned_stake = Column(Integer, nullable=True)
    opponent_id = Column(String(64), nullable=True)
    is_capped = Column(Boolean, default=True)  
    
    def __repr__(self):
        return f"<StakeInfo(player_id={self.player_id}, max_stake={self.max_stake})>"
