from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, text, Text
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

# TeamRegistration lived here until the `dropteamreg01` migration. It was the old
# league's roster table (league.py, deleted in 81318df "league cleanup"), keyed
# 1:1 to a globally-unique Team name. Tournament rosters replaced it — see
# models/tournament.py TournamentTeamMember, which scopes a roster to one
# tournament instead of to a team name shared across every guild.


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
