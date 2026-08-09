"""complete sign_up_history, then backfill missing display names

Two-step data fill. First, sign_up_history (live event recording began
2025-08) is completed backward by synthesizing 'join' events from the
final-roster sign_ups JSON of older sessions -- making the event table THE
one historical signup record instead of a second source of truth. Then
missing player_stats display names fill from the best source: the player's
name in another guild's player_stats, else their latest sign_up_history
event. Players in neither resolve at display time via live Discord member
lookup. Data-only; idempotent; safe on any deployment (purely
self-relative). Downgrade is a no-op.

Revision ID: dispnamefill0
Revises: cardlend0col
Create Date: 2026-08-08
"""
from alembic import op

from helpers.legacy_import import (
    backfill_missing_display_names,
    backfill_sign_up_history,
)

revision = "dispnamefill0"
down_revision = "cardlend0col"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    backfill_sign_up_history(conn)
    backfill_missing_display_names(conn)


def downgrade():
    # One-way data fill; nothing to reverse.
    pass
