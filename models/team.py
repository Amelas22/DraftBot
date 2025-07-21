from sqlalchemy import Column, Integer, String, DateTime, JSON, ForeignKey, text, Text
from sqlalchemy.orm import relationship
from database.models_base import Base

class Team(Base):
    __tablename__ = 'teams'

    TeamID = Column(Integer, primary_key=True)
    TeamName = Column(String(128), unique=True, nullable=False)
    MatchesCompleted = Column(Integer, nullable=True)
    MatchWins = Column(Integer, nullable=True)
    PointsEarned = Column(Integer, nullable=True)
    PreseasonPoints = Column(Integer, default=0, server_default=text('0'))
    
    # Add relationships
    weekly_limits = relationship("WeeklyLimit", back_populates="team")

class TeamRegistration(Base):
    __tablename__ = 'team_registration'

    ID = Column(Integer, primary_key=True, nullable=False, autoincrement=True)
    TeamID = Column(Integer)
    TeamName = Column(String(128), unique=True, nullable=False)
    TeamMembers = Column(JSON)

    # Add relationship
    # team = relationship("Team")  # Commented out - no FK in production

class WeeklyLimit(Base):
    __tablename__ = 'weekly_limits'

    ID = Column(Integer, primary_key=True, nullable=True, autoincrement=True)
    TeamID = Column(Integer, ForeignKey('teams.TeamID'))
    TeamName = Column(Text, nullable=False)
    WeekStartDate = Column(DateTime, nullable=False)
    MatchesPlayed = Column(Integer, default=0, server_default=text('0'))
    PointsEarned = Column(Integer, default=0, server_default=text('0'))

    # Add relationship
    team = relationship("Team", back_populates="weekly_limits")
