from sqlalchemy import Column, Integer, String, DateTime, Index
from datetime import datetime
from database.models_base import Base


class DebtLedger(Base):
    """
    Ledger-style debt tracking table.

    Each debt or settlement creates TWO entries (one from each player's perspective).
    Balance is calculated by SUM(amount) for a player-counterparty pair.

    Amount convention:
    - Positive: owed TO the player (they are owed money)
    - Negative: player OWES (they owe money)

    source_id usage:
    - 'draft': session_id - links to the DraftSession
    - 'settlement': generated UUID - groups the two settlement entries together
    - 'admin': generated UUID - groups the two admin adjustment entries together

    Example - Alice owes Bob 30 tix from Draft #1234:
        Entry 1: player=Alice, counterparty=Bob, amount=-30, source_type='draft', source_id='session123'
        Entry 2: player=Bob, counterparty=Alice, amount=+30, source_type='draft', source_id='session123'

    Settlement - Alice pays Bob 30 tix:
        Entry 1: player=Alice, counterparty=Bob, amount=+30, source_type='settlement', source_id='uuid-xxx'
        Entry 2: player=Bob, counterparty=Alice, amount=-30, source_type='settlement', source_id='uuid-xxx'
    """
    __tablename__ = 'debt_ledger'

    id = Column(Integer, primary_key=True, nullable=False, autoincrement=True)
    guild_id = Column(String(64), nullable=False, index=True)

    # Ledger entry (from player's perspective)
    player_id = Column(String(64), nullable=False, index=True)
    counterparty_id = Column(String(64), nullable=False, index=True)
    amount = Column(Integer, nullable=False)  # + owed TO player, - player OWES

    # Source/audit info
    source_type = Column(String(32), nullable=False)  # 'draft', 'settlement', 'admin'
    source_id = Column(String(64), nullable=True)  # session_id for drafts
    notes = Column(String(256), nullable=True)  # Human-readable context

    # Multi-entity support: NULL = the entry is tix (all pre-existing rows);
    # set = the entry is copies of this card and `amount` is the signed
    # quantity (same sign convention as tix). See /lend design spec.
    card_name = Column(String(128), nullable=True)

    # How a settlement row was paid: 'wallet' (auto-drawn from the payer's wallet by
    # mtgo_resolution_service) or 'external' (tix moved in MTGO and someone recorded it
    # afterwards). NULL on every non-settlement row -- and on nothing else, because the
    # backfill below classifies all history.
    settlement_method = Column(String(16), nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.now)
    created_by = Column(String(64), nullable=True)  # Who recorded this entry

    # Composite index for balance queries
    __table_args__ = (
        Index('ix_debt_ledger_balance_lookup', 'guild_id', 'player_id', 'counterparty_id'),
    )

    def __repr__(self):
        return f"<DebtLedger(player={self.player_id}, counterparty={self.counterparty_id}, amount={self.amount}, type={self.source_type})>"
