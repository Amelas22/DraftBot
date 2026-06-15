from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)

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
    # 'swiss' (re-pair each round) | 'round_robin' | 'manual' (schedule fixed upfront)
    format = Column(String(16), nullable=False, default='swiss',
                    server_default=text("'swiss'"))
    created_at = Column(DateTime, default=datetime.now)
    # Where the auto-updating standings message lives (edited in place on every result)
    standings_channel_id = Column(String(64), nullable=True)
    standings_message_id = Column(String(64), nullable=True)

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

    # This tournament's standings (never written onto the global Team record)
    match_wins = Column(Integer, nullable=False, default=0, server_default=text('0'))
    match_losses = Column(Integer, nullable=False, default=0, server_default=text('0'))
    match_draws = Column(Integer, nullable=False, default=0, server_default=text('0'))
    points = Column(Integer, nullable=False, default=0, server_default=text('0'))
    game_wins = Column(Integer, nullable=False, default=0, server_default=text('0'))
    game_losses = Column(Integer, nullable=False, default=0, server_default=text('0'))
    byes = Column(Integer, nullable=False, default=0, server_default=text('0'))

    __table_args__ = (
        UniqueConstraint('tournament_id', 'team_id', name='uq_tournament_team'),
    )

    def __repr__(self):
        return f"<TournamentParticipant(tournament_id={self.tournament_id}, team={self.team_name!r})>"


class TournamentRound(Base):
    __tablename__ = 'tournament_rounds'

    id = Column(Integer, primary_key=True, autoincrement=True)
    tournament_id = Column(Integer, ForeignKey('tournaments.id'), nullable=False)
    round_number = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    # Where this round's pairings message lives, for view re-registration on restart
    pairings_channel_id = Column(String(64), nullable=True)
    pairings_message_id = Column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint('tournament_id', 'round_number', name='uq_tournament_round'),
    )

    def __repr__(self):
        return f"<TournamentRound(tournament_id={self.tournament_id}, round={self.round_number})>"


class TournamentMatch(Base):
    __tablename__ = 'tournament_matches'

    id = Column(Integer, primary_key=True, autoincrement=True)
    round_id = Column(Integer, ForeignKey('tournament_rounds.id'), nullable=False)
    team_a_participant_id = Column(Integer, ForeignKey('tournament_participants.id'), nullable=False)
    # Null for a bye "match" (team A gets the bye)
    team_b_participant_id = Column(Integer, ForeignKey('tournament_participants.id'), nullable=True)
    team_a_wins = Column(Integer, nullable=True)
    team_b_wins = Column(Integer, nullable=True)
    is_bye = Column(Boolean, nullable=False, default=False, server_default=text('0'))
    # Each match has its own pairing message (with the Play button) for restart
    # re-registration, and a per-match thread the lobby runs in.
    pairings_channel_id = Column(String(64), nullable=True)
    pairings_message_id = Column(String(64), nullable=True)
    thread_id = Column(String(64), nullable=True)

    def __repr__(self):
        return (f"<TournamentMatch(round_id={self.round_id}, "
                f"a={self.team_a_participant_id}, b={self.team_b_participant_id})>")
