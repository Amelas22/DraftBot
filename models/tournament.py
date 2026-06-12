from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, text

from database.models_base import Base


class Tournament(Base):
    __tablename__ = 'tournaments'

    id = Column(Integer, primary_key=True, autoincrement=True)
    guild_id = Column(String(64), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    total_rounds = Column(Integer, nullable=False)
    current_round = Column(Integer, nullable=False, default=0, server_default=text('0'))
    status = Column(String(16), nullable=False, default='registration',
                    server_default=text("'registration'"))
    created_at = Column(DateTime, default=datetime.now)

    def __repr__(self):
        return f"<Tournament(id={self.id}, name={self.name!r}, status={self.status})>"


class TournamentParticipant(Base):
    __tablename__ = 'tournament_participants'

    id = Column(Integer, primary_key=True, autoincrement=True)
    tournament_id = Column(Integer, ForeignKey('tournaments.id'), nullable=False)
    # Loose reference to teams.TeamID (no FK), matching the Match convention
    team_id = Column(Integer, nullable=False)
    team_name = Column(String(128), nullable=False)
    captain_user_id = Column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint('tournament_id', 'team_id', name='uq_tournament_team'),
    )

    def __repr__(self):
        return f"<TournamentParticipant(tournament_id={self.tournament_id}, team={self.team_name!r})>"
