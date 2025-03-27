from sqlalchemy import Column, Integer, String, DateTime, JSON, ForeignKey
from sqlalchemy.orm import relationship
from database.models_base import Base

class Team(Base):
    __tablename__ = 'teams'

    TeamID = Column(Integer, primary_key=True)
    TeamName = Column(String(128), unique=True, nullable=False)
    MatchesCompleted = Column(Integer, default=0)
    MatchWins = Column(Integer, default=0)
    PointsEarned = Column(Integer, default=0)
    PreseasonPoints = Column(Integer, default=0)
    
    # Add relationships
    weekly_limits = relationship("WeeklyLimit", back_populates="team")

class TeamRegistration(Base):
    __tablename__ = 'team_registration'

    ID = Column(Integer, primary_key=True)
    TeamID = Column(Integer, ForeignKey('teams.TeamID'))
    TeamName = Column(String(128), unique=True, nullable=False)
    TeamMembers = Column(JSON)

    # Add relationship
    team = relationship("Team")

class WeeklyLimit(Base):
    __tablename__ = 'weekly_limits'

    ID = Column(Integer, primary_key=True)
    TeamID = Column(Integer, ForeignKey('teams.TeamID'))
    TeamName = Column(String(128), nullable=False)
    WeekStartDate = Column(DateTime, nullable=False)
    MatchesPlayed = Column(Integer, default=0)
    PointsEarned = Column(Integer, default=0)

    # Add relationship
    team = relationship("Team", back_populates="weekly_limits")
