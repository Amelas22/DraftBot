"""backfill trueskill to include premade and draw-prob-0

Recomputes player_stats.true_skill_mu/sigma and games_won/lost from the full
random+staked+premade 1v1 match history (chronological per guild), using the
shared draw-probability-0 environment. Data-only migration: no schema change.
Downgrade is a no-op (the prior values were a strict subset and cannot be
reconstructed).

Revision ID: tskill0backfl
Revises: c4pture10g5at
Create Date: 2026-07-01
"""
from alembic import op

from helpers.skill import backfill_skill_ratings

revision = "tskill0backfl"
down_revision = "c4pture10g5at"
branch_labels = None
depends_on = None


def upgrade():
    backfill_skill_ratings(op.get_bind())


def downgrade():
    # One-way data recompute; nothing to reverse.
    pass
