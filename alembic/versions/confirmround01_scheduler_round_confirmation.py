"""scheduler: per-player round confirmations and the usual-week switch

Additive: two new tables, nothing existing is altered. The backfill only
inserts: every player with a day override in the current or next league week
gets that week marked confirmed, so existing this-week edits keep counting.

Revision ID: confirmround01
Revises: round1dl01
Create Date: 2026-11-16
"""
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import Connection

revision = 'confirmround01'
down_revision = 'round1dl01'
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

# Frozen copy of the league clock: weeks run Monday 00:00 to Monday 00:00 Pacific.
_LEAGUE_ZONE = ZoneInfo("America/Los_Angeles")


def _pacific_monday(instant_utc: datetime) -> date:
    local = instant_utc.astimezone(_LEAGUE_ZONE).date()
    return local - timedelta(days=local.weekday())


def backfill_confirmations(conn: Connection, now_utc: datetime) -> int:
    """Confirm the current and next league week for each override's weeks; returns rows inserted."""
    current = _pacific_monday(now_utc)
    wanted = {current, current + timedelta(days=7)}
    rows = conn.execute(sa.text(
        "SELECT user_id, local_date, zone FROM player_day_override")).fetchall()
    inserted = 0
    for user_id, local_date, zone in rows:
        day = date.fromisoformat(str(local_date)[:10])
        tz = ZoneInfo(zone)
        start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)
        end = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(days=1)
        end = end.astimezone(timezone.utc) - timedelta(seconds=1)
        weeks = {_pacific_monday(start), _pacific_monday(end)} & wanted
        for monday in sorted(weeks):
            result = conn.execute(sa.text(
                "INSERT OR IGNORE INTO player_round_confirmation "
                "(user_id, round_monday, source, stored_utc) "
                "VALUES (:u, :m, 'backfill', :t)"),
                {"u": user_id, "m": monday.isoformat(),
                 "t": now_utc.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")})
            inserted += result.rowcount
    return inserted


def upgrade():
    op.create_table(
        'player_scheduler_setting',
        sa.Column('user_id', sa.String(64), primary_key=True),
        sa.Column('trusts_usual', sa.Boolean(), nullable=True),
    )
    op.create_table(
        'player_round_confirmation',
        sa.Column('user_id', sa.String(64), primary_key=True),
        sa.Column('round_monday', sa.Date(), primary_key=True),
        sa.Column('source', sa.String(16), nullable=False),
        sa.Column('stored_utc', sa.DateTime(), nullable=False),
    )
    count = backfill_confirmations(op.get_bind(), datetime.now(timezone.utc))
    log.info("confirmround01: confirmed %d player-week(s) from existing day overrides", count)


def downgrade():
    op.drop_table('player_round_confirmation')
    op.drop_table('player_scheduler_setting')
