"""import the old bot's match history and unify the old server's guild

legacy_data/*.csv hold the previous server's 10,517 played matches
(2024-03-16 -> 2025-03-08), exported from the community's old bot at the
March 2025 server move and until now only merged into /stats displays.
This imports them into the match_results ledger as 'staked' sessions
directly under the current guild id (session ids prefixed 'legacy-'; no
stake_info rows), moves the old server's 824 native DraftBot matches to the
current guild the same way, and re-runs the rating backfill over the now
continuous ledger. The /stats display-time CSV merge is retired in the same
change — the DB itself now holds this history, once.

Guarded for other deployments of this codebase: databases with no sessions
under either of this community's guild ids (fresh installs, other servers)
skip the import entirely. Idempotent; downgrade is a no-op.

Revision ID: legacyimport0
Revises: skillheal0rpt
Create Date: 2026-08-04
"""
from pathlib import Path

from alembic import op

from helpers.legacy_import import (
    community_data_present,
    import_legacy_history,
    migrate_guild_history,
)
from helpers.skill import backfill_skill_ratings

revision = "legacyimport0"
down_revision = "skillheal0rpt"
branch_labels = None
depends_on = None

CSV_DIR = Path(__file__).resolve().parents[2] / "legacy_data"


def upgrade():
    conn = op.get_bind()
    if not community_data_present(conn):
        return
    import_legacy_history(conn, CSV_DIR)  # raises if the tracked CSVs are missing
    migrate_guild_history(conn)
    backfill_skill_ratings(conn)


def downgrade():
    # One-way data import + recompute; nothing to reverse.
    pass
