from sqlalchemy import Column, Integer, String, ForeignKey, Index
from database.models_base import Base

class StakePairing(Base):
    """
    Represents a calculated stake pairing between two players in a draft.

    Multiple pairings can exist per player per session.
    Each pairing represents a bilateral stake agreement calculated by the stake algorithm.

    This table stores OUTPUT data (calculated assignments) while stake_info stores
    INPUT data (player preferences). This separation prevents data loss when a player
    has multiple stake opponents.
    """
    __tablename__ = 'stake_pairings'

    id = Column(Integer, primary_key=True, nullable=False, autoincrement=True)
    session_id = Column(String(64), ForeignKey('draft_sessions.session_id'), nullable=False)
    player_a_id = Column(String(64), nullable=False)
    player_b_id = Column(String(64), nullable=False)
    amount = Column(Integer, nullable=False)
    # Which side each party backed. Recorded when the pairing is written, where
    # team_a/team_b are already in scope, rather than re-derived at settlement
    # from roster membership -- an inference that cannot express a backer who is
    # on neither roster.
    side_a = Column(String(1), nullable=True)
    side_b = Column(String(1), nullable=True)

    __table_args__ = (
        # Index for efficient queries by session
        Index('ix_stake_pairings_session', 'session_id'),
        # Index for finding pairings involving a specific player
        Index('ix_stake_pairings_player_a', 'player_a_id'),
        Index('ix_stake_pairings_player_b', 'player_b_id'),
    )

    def resolve(self, winning_side: str) -> tuple[str, str] | None:
        """(winner_id, loser_id) for this wager, or None if it is not settleable.

        The single answer to "who won this wager": settlement and the bet-outcomes
        display both read it here, so they cannot drift apart.

        A wager needs exactly one party on each side. Everything else -- both parties
        on the same side, or a half-recorded row with one side missing -- is not a
        wager and resolves to None rather than defaulting a winner.
        """
        if {self.side_a, self.side_b} != {"A", "B"}:
            return None
        if self.side_a == winning_side:
            return self.player_a_id, self.player_b_id
        return self.player_b_id, self.player_a_id

    def __repr__(self):
        return f"<StakePairing(player_a={self.player_a_id}, player_b={self.player_b_id}, amount={self.amount})>"
