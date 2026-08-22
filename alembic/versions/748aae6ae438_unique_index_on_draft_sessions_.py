"""unique index on draft_sessions.tournament_match_id

Task 5b fix round 1: the creation-time guard in sessions/premade_session.py
narrows the window for two open cube pickers to create two drafts linked to
the same tournament match, but it can't close it -- two submits milliseconds
apart both pass the guard's query. This index makes the invariant hold in
the database regardless of which code path writes the column.

SQLite permits multiple NULLs in a unique index, so the overwhelming
majority of drafts (non-tournament, tournament_match_id NULL) are
unaffected. Verified against a production copy: 16 linked drafts, zero
duplicate match ids, so this applies cleanly.

Revision ID: 748aae6ae438
Revises: a81fdb1d60de
Create Date: 2026-08-20 04:13:52.323839

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '748aae6ae438'
down_revision: Union[str, Sequence[str], None] = 'a81fdb1d60de'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        'ix_draft_sessions_tournament_match_id',
        'draft_sessions',
        ['tournament_match_id'],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_draft_sessions_tournament_match_id', table_name='draft_sessions')
