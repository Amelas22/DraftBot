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

    __table_args__ = (
        # Index for efficient queries by session
        Index('ix_stake_pairings_session', 'session_id'),
        # Index for finding pairings involving a specific player
        Index('ix_stake_pairings_player_a', 'player_a_id'),
        Index('ix_stake_pairings_player_b', 'player_b_id'),
    )

    def __repr__(self):
        return f"<StakePairing(player_a={self.player_a_id}, player_b={self.player_b_id}, amount={self.amount})>"
