from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, and_, or_
from sqlalchemy.orm import relationship
from sqlalchemy import select
from datetime import datetime

from database.models_base import Base
from database.db_session import db_session

class Match(Base):
    __tablename__ = 'matches'

    MatchID = Column(Integer, primary_key=True)
    TeamAID = Column(Integer)
    TeamBID = Column(Integer)
    TeamAWins = Column(Integer, default=0)
    TeamBWins = Column(Integer, default=0)
    DraftWinnerID = Column(Integer, default=None)
    MatchDate = Column(DateTime, default=datetime.now)  # Changed to callable
    TeamAName = Column(String(128))
    TeamBName = Column(String(128))

    def __repr__(self):
        return f"<Match(MatchID={self.MatchID}, TeamA={self.TeamAName}, TeamB={self.TeamBName})>"

    @classmethod
    async def get_by_id(cls, match_id: int):
        """Get a match by its ID"""
        async with db_session() as session:
            query = select(cls).filter_by(MatchID=match_id)
            result = await session.execute(query)
            return result.scalar_one_or_none()

class MatchResult(Base):
    __tablename__ = 'match_results'

    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), ForeignKey('draft_sessions.session_id'))
    match_number = Column(Integer)
    player1_id = Column(String(64))
    player1_wins = Column(Integer, default=0)
    player2_id = Column(String(64))
    player2_wins = Column(Integer, default=0)
    winner_id = Column(String(64), nullable=True)
    pairing_message_id = Column(String(64))
    guild_id = Column(String(64))
    
    # Relationship with DraftSession
    draft_session = relationship("DraftSession", back_populates="match_results")

    def __repr__(self):
        return f"<MatchResult(id={self.id}, match_number={self.match_number}, winner={self.winner_id})>"

    @classmethod
    async def find_unreported_for_user(cls, session_id: str, user_id: str):
        """Find earliest unreported match for a user in a specific session"""
        async with db_session() as session:
            stmt = select(cls).where(
                and_(
                    cls.session_id == session_id,
                    or_(
                        cls.player1_id == user_id,
                        cls.player2_id == user_id
                    ),
                    cls.winner_id == None
                )
            ).order_by(cls.match_number)
            
            result = await session.execute(stmt)
            return result.scalars().first()

    @classmethod
    async def get_by_id(cls, result_id: int):
        """Get a match result by its ID"""
        async with db_session() as session:
            query = select(cls).filter_by(id=result_id)
            result = await session.execute(query)
            return result.scalar_one_or_none()

    async def update_result(self, **kwargs):
        """Update match result with new values"""
        async with db_session() as session:
            for key, value in kwargs.items():
                if hasattr(self, key):
                    setattr(self, key, value)
            session.add(self) 