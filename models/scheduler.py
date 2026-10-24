"""Scheduler tables. Timestamps here are naive UTC (hence the ``_utc`` suffix);
the rest of drafts.db stores naive New York time."""
from sqlalchemy import Column, DateTime, Integer, LargeBinary, String

from database.models_base import Base


class PlayerAvailability(Base):
    """A player's usual week: 336 half-hour cells as a 42-byte bitmap."""

    __tablename__ = 'player_availability'

    user_id = Column(String(64), primary_key=True)
    zone = Column(String(64), nullable=False)
    cells = Column(LargeBinary(42), nullable=False)
    version = Column(Integer, nullable=False)
    stored_utc = Column(DateTime, nullable=False)


class SchedulerLink(Base):
    """A link handed to a player; its token is the bearer credential for the scheduler page."""

    __tablename__ = 'scheduler_link'

    id = Column(Integer, primary_key=True)
    token = Column(String(32), unique=True, nullable=False)
    user_id = Column(String(64), nullable=False, index=True)
    guild_id = Column(String(64), nullable=False)
    view = Column(String(16), nullable=False)
    issued_utc = Column(DateTime, nullable=False)
    expires_utc = Column(DateTime, nullable=False, index=True)
    removed_utc = Column(DateTime, nullable=True)
