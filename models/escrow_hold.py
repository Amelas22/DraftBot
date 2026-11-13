"""A player's tix locked for a named counterparty until a moderator decides."""
from datetime import datetime

from sqlalchemy import Column, DateTime, Index, Integer, String

from database.models_base import Base

OPEN = "open"
RELEASED = "released"  # paid on to the counterparty
RETURNED = "returned"  # handed back to the funder


class EscrowHold(Base):
    """The tix themselves sit with the synthetic holder ``escrow:hold:<id>``; this row
    is the who, why and where-it-stands."""

    __tablename__ = 'escrow_holds'

    id = Column(Integer, primary_key=True)
    guild_id = Column(String(64), nullable=False)
    funder_id = Column(String(64), nullable=False)
    counterparty_id = Column(String(64), nullable=False)
    amount = Column(Integer, nullable=False)
    note = Column(String(200), nullable=False)
    status = Column(String(16), nullable=False, default=OPEN)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    decided_by = Column(String(64), nullable=True)
    decided_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('ix_escrow_holds_guild_status', 'guild_id', 'status'),
    )
