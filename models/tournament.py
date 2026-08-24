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
    # Escrow entry fee in tix (0 = free, the default = today's behavior). When > 0, a
    # registering captain must have the fee held in their wallet before the team counts
    # as registered (see services/tournament_escrow_service.py).
    entry_fee = Column(Integer, nullable=False, default=0, server_default=text('0'))
    # How the prize pool is split at payout, declared up front so entrants know it before
    # they pay: 'winner_take_all' | 'top2' | 'top3' | 'top4' (see PAYOUT_STRUCTURES).
    payout_structure = Column(String(32), nullable=False, default='winner_take_all',
                              server_default=text("'winner_take_all'"))
    # Top-N cut declared at creation. NULL = no cut (today's behaviour). The
    # bracket is seeded from final swiss standings; see draft_organization/bracket.py.
    cut_to = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    # Where the auto-updating standings message lives (edited in place on every result)
    standings_channel_id = Column(String(64), nullable=True)
    standings_message_id = Column(String(64), nullable=True)
    # The live registration board (roster + who has paid), posted at creation and
    # edited in place until the tournament starts. Same shape as standings_*.
    board_channel_id = Column(String(64), nullable=True)
    board_message_id = Column(String(64), nullable=True)

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

    # Escrow gate. 'paid' = entry fee is held (or it's a free tournament, or a row that
    # predates escrow — server_default grandfathers those); 'pending' = registered but the
    # escrow isn't secured yet. Only 'paid' participants are seeded when the tournament starts.
    status = Column(String(16), nullable=False, default='paid', server_default=text("'paid'"))
    # The captain's WalletTx reserve holding the fee — cancelled to refund (drop before
    # start), or settled into the tournament's prize wallet when it starts. Null for
    # free / grandfathered / comped participants.
    paid_at = Column(DateTime, nullable=True)

    # This tournament's standings (never written onto the global Team record)
    match_wins = Column(Integer, nullable=False, default=0, server_default=text('0'))
    match_losses = Column(Integer, nullable=False, default=0, server_default=text('0'))
    match_draws = Column(Integer, nullable=False, default=0, server_default=text('0'))
    points = Column(Integer, nullable=False, default=0, server_default=text('0'))
    game_wins = Column(Integer, nullable=False, default=0, server_default=text('0'))
    game_losses = Column(Integer, nullable=False, default=0, server_default=text('0'))
    byes = Column(Integer, nullable=False, default=0, server_default=text('0'))
    # Seed stamped once, at the cut, from rank_standings. NULL for teams that
    # missed the cut and for tournaments with no cut. Stored rather than
    # recomputed because it is the number players were told, and it is what
    # makes 3rd/4th well-defined between two semifinal losers.
    seed = Column(Integer, nullable=True)
    # The team's Discord role for THIS tournament. NULL means no role: the
    # tournament has not started, predates this feature, or has completed and
    # had its roles deleted. Stored as an id rather than a name so cleanup
    # deletes the role this team actually got, not whatever currently answers
    # to its name -- Discord permits duplicate role names, and two concurrent
    # tournaments may each have an "Alpha".
    role_id = Column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint('tournament_id', 'team_id', name='uq_tournament_team'),
    )

    def __repr__(self):
        return f"<TournamentParticipant(tournament_id={self.tournament_id}, team={self.team_name!r})>"


class TournamentTeamMember(Base):
    """A player on a team's roster, for one tournament.

    Scoped to the participant rather than to the global Team identity on purpose:
    teams.TeamName is unique across the whole database with no guild column, so a
    roster hung off the Team row would merge two servers that happen to pick the
    same team name, and would rewrite a finished tournament's roster whenever the
    team re-entered a later one.

    The captain is NOT stored here. tournament_participants.captain_user_id stays
    the single authority for who owns the team (it is who escrow charges and who
    payouts go to); duplicating them into the roster would give money-bearing state
    two places to disagree.
    """

    __tablename__ = 'tournament_team_members'

    id = Column(Integer, primary_key=True, autoincrement=True)
    participant_id = Column(Integer, ForeignKey('tournament_participants.id'),
                            nullable=False, index=True)
    user_id = Column(String(64), nullable=False, index=True)
    # Snapshot of the name at the time they were added. Boards render <@user_id> so
    # Discord always resolves the current name; this is the fallback for members who
    # have since left the guild, and the readable record in logs and exports.
    display_name = Column(String(128), nullable=False)
    added_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint('participant_id', 'user_id', name='uq_participant_member'),
    )

    def __repr__(self):
        return (f"<TournamentTeamMember(participant_id={self.participant_id}, "
                f"user_id={self.user_id})>")


# A round's stage. Named here rather than in the service layer so the column
# default and every predicate that reads it share one spelling -- a service-side
# constant would be a circular import back into this module.
STAGE_SWISS = 'swiss'
STAGE_PLAYOFF = 'playoff'


class TournamentRound(Base):
    __tablename__ = 'tournament_rounds'

    id = Column(Integer, primary_key=True, autoincrement=True)
    tournament_id = Column(Integer, ForeignKey('tournaments.id'), nullable=False)
    round_number = Column(Integer, nullable=False)
    # 'swiss' | 'playoff'. The single fact that freezes swiss records: a result
    # on a playoff round skips the participant-stat update entirely.
    stage = Column(String(16), nullable=False, default=STAGE_SWISS,
                   server_default=text(f"'{STAGE_SWISS}'"))
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
    # The pinned per-match control message inside that thread. Nullable: matches
    # created before this feature (and any match nobody has opened yet) have none,
    # and the Play button fills it in on first click.
    control_message_id = Column(String(64), nullable=True)

    def __repr__(self):
        return (f"<TournamentMatch(round_id={self.round_id}, "
                f"a={self.team_a_participant_id}, b={self.team_b_participant_id})>")
