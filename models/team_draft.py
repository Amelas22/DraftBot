"""Team-draft matchups. Timestamps are naive UTC (hence the ``_utc`` suffix)."""
from sqlalchemy import Column, DateTime, Integer, JSON, String, Text

from database.models_base import Base


class TeamDraft(Base):
    """A matchup between two sides, each a set of roles and/or individual users (id strings)."""

    __tablename__ = 'team_draft'

    id = Column(Integer, primary_key=True)
    guild_id = Column(String(64), nullable=False)
    channel_id = Column(String(64), nullable=False)
    thread_id = Column(String(64), nullable=True)
    control_message_id = Column(String(64), nullable=True)
    name = Column(String(100), nullable=False)
    side_a_roles = Column(JSON, nullable=False, default=list)
    side_a_users = Column(JSON, nullable=False, default=list)
    side_b_roles = Column(JSON, nullable=False, default=list)
    side_b_users = Column(JSON, nullable=False, default=list)
    created_by = Column(String(64), nullable=False)
    created_utc = Column(DateTime, nullable=False)
    booked_start_utc = Column(DateTime, nullable=True)
    booked_by = Column(String(64), nullable=True)
    booked_by_name = Column(String(100), nullable=True)
    booking_version = Column(Integer, nullable=False, default=0)
    draft_session_id = Column(String(64), nullable=True, unique=True)


class TeamDraftAction(Base):
    """An already-applied action id with its stored result, for idempotent retries."""

    __tablename__ = 'team_draft_action'

    action_id = Column(String(64), primary_key=True)
    user_id = Column(String(64), nullable=False)
    result_json = Column(Text, nullable=False)
    done_utc = Column(DateTime, nullable=False)
