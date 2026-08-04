"""re-heal trueskill: rate real post-2021 snowflakes, undo re-report double counts

The tskill0backfl backfill treated every id >= TEST_USER_ID_BASE (9e17) as a
synthetic test user, but real Discord accounts created after ~late 2021 also
live above 9e17 — their games were silently dropped (some players were wiped
to zero). Separately, the live result path re-applied a full rating update on
every re-selection of a result, double-counting games. _is_test_user is now
bounded to the synthetic allocator band and the live path is guarded, so one
fresh replay of the intact match_results ledger heals player_stats exactly.
Data-only migration: no schema change. Downgrade is a no-op.

Revision ID: skillheal0rpt
Revises: trophreguess0
Create Date: 2026-08-04
"""
from alembic import op

from helpers.skill import backfill_skill_ratings

revision = "skillheal0rpt"
down_revision = "trophreguess0"
branch_labels = None
depends_on = None


def upgrade():
    backfill_skill_ratings(op.get_bind())


def downgrade():
    # One-way data recompute; nothing to reverse.
    pass
