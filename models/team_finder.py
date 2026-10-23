"""Find-a-team tables. Timestamps are naive UTC (hence the ``_utc`` suffix)."""
from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text

from database.models_base import Base


class LfgTeam(Base):
    """A team a captain has opened; the captain also has a member row."""

    __tablename__ = 'lfg_team'

    id = Column(Integer, primary_key=True)
    guild_id = Column(String(64), nullable=False, index=True)
    captain_id = Column(String(64), nullable=False, index=True)
    size = Column(Integer, nullable=False)
    note = Column(String(200), nullable=False, default='')
    taking_requests = Column(Boolean, nullable=False, default=True)
    status = Column(String(16), nullable=False, default='open')  # open | full | closed
    created_utc = Column(DateTime, nullable=False)
    closed_utc = Column(DateTime, nullable=True)


class LfgTeamMember(Base):
    __tablename__ = 'lfg_team_member'

    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, nullable=False, index=True)
    user_id = Column(String(64), nullable=False, index=True)
    display_name = Column(String(100), nullable=False)
    joined_utc = Column(DateTime, nullable=False)
    left_utc = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('uq_lfg_member_current', 'team_id', 'user_id', unique=True,
              sqlite_where=left_utc.is_(None)),
    )


class LfgRequest(Base):
    __tablename__ = 'lfg_request'

    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, nullable=False, index=True)
    user_id = Column(String(64), nullable=False, index=True)
    display_name = Column(String(100), nullable=False)
    note = Column(String(200), nullable=False, default='')
    status = Column(String(16), nullable=False, default='pending')  # pending | accepted | declined | withdrawn | filled
    created_utc = Column(DateTime, nullable=False)
    answered_utc = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('uq_lfg_request_pending', 'team_id', 'user_id', unique=True,
              sqlite_where=status == 'pending'),
    )


class LfgAction(Base):
    """An already-applied action id with its stored result, for idempotent retries."""

    __tablename__ = 'lfg_action'

    action_id = Column(String(64), primary_key=True)
    user_id = Column(String(64), nullable=False)
    result_json = Column(Text, nullable=False)
    done_utc = Column(DateTime, nullable=False)
