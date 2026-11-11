"""count return reminders and record when the mods were told a deck is overdue

Additive: two nullable columns. Every existing loan is NULL in both, which reads
as "no reminder counted yet" and "mods not told", so a deck already out starts
counting from here and is flagged once it has been asked for again.

Revision ID: loanoverdue01
Revises: libhold01
Create Date: 2026-11-10
"""
from alembic import op
import sqlalchemy as sa

revision = 'loanoverdue01'
down_revision = 'libhold01'
branch_labels = None
depends_on = None

_COLUMNS = (
    ('reminders_sent', sa.Integer()),
    ('overdue_posted_at', sa.DateTime()),
)


def _loan_columns():
    bind = op.get_bind()
    return {c['name'] for c in sa.inspect(bind).get_columns('card_loans')}


def upgrade():
    present = _loan_columns()
    for name, kind in _COLUMNS:
        if name not in present:
            op.add_column('card_loans', sa.Column(name, kind, nullable=True))


def downgrade():
    present = _loan_columns()
    with op.batch_alter_table('card_loans', schema=None) as batch_op:
        for name, _ in _COLUMNS:
            if name in present:
                batch_op.drop_column(name)
