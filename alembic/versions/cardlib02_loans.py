"""card loans: a deck out of the library, and the trades that move it

A loan is one drafter's pool, assigned when the draft ends and collected when
they ask for it. The cards live in another MTGO account, so every state change
is half of a distributed operation: DraftBot holds the pending state, the
serve runs the trade, and the job's terminal state settles it. The claim only
moves when a job finishes -- marking somebody as holding a deck the moment we
ASK for the trade would assert a handover MTGO may never make.

`library_id` is stamped at assignment, so a deck stays priced and stocked by
the shelf it came out of even if its server is later rebound somewhere else.

One active loan per borrower, ANYWHERE: the library is a single MTGO account,
so a borrower holding a deck is holding its only copies of those cards, and
being in a second server does not entitle them to a second deck. The partial
index names the two FINISHED states rather than listing the active ones, so a
state added later is active by default -- the safe direction for a state that
means "we do not know".

`mtgo_jobs` gains two columns. `library_id` says whose cards a trade moved,
because one account holds every library's stock commingled. `order_id` says
which trades were one order: a deck too big for a single trade becomes
several, and a loan only comes to rest once none of them is still open.

Revision ID: cardlib02
Revises: cardlib01
Create Date: 2026-09-23
"""
import sqlalchemy as sa
from alembic import op

revision = 'cardlib02'
down_revision = 'cardlib01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'card_loans',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('guild_id', sa.String(64), nullable=False),
        sa.Column('library_id', sa.String(64), nullable=True),
        sa.Column('borrower_id', sa.String(64), nullable=False),
        sa.Column('cards', sa.JSON(), nullable=False),
        sa.Column('pending_cards', sa.JSON(), nullable=True),
        sa.Column('state', sa.String(16), nullable=False,
                  server_default=sa.text("'assigned'")),
        sa.Column('job_id', sa.String(64), nullable=True),
        sa.Column('source', sa.String(128), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('borrowed_at', sa.DateTime(), nullable=True),
        sa.Column('returned_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_card_loans_borrower', 'card_loans',
                    ['guild_id', 'borrower_id'])
    op.create_index('uq_card_loans_one_active_per_borrower', 'card_loans',
                    ['borrower_id'], unique=True,
                    sqlite_where=sa.text("state NOT IN ('returned', 'expired')"))
    op.create_index('ix_card_loans_job', 'card_loans', ['job_id'],
                    sqlite_where=sa.text('job_id IS NOT NULL'))

    op.add_column('mtgo_jobs', sa.Column('library_id', sa.String(64), nullable=True))
    op.add_column('mtgo_jobs', sa.Column('order_id', sa.String(64), nullable=True))
    op.add_column('mtgo_jobs', sa.Column('card_name', sa.String(128), nullable=True))
    # Named as SQLAlchemy's `index=True` names it, so autogenerate does not
    # see a stranger and propose dropping one to create the other.
    op.create_index('ix_mtgo_jobs_order_id', 'mtgo_jobs', ['order_id'])


def downgrade() -> None:
    op.drop_index('ix_mtgo_jobs_order_id', table_name='mtgo_jobs')
    for column in ('card_name', 'order_id', 'library_id'):
        op.drop_column('mtgo_jobs', column)
    for index in ('ix_card_loans_job', 'uq_card_loans_one_active_per_borrower',
                  'ix_card_loans_borrower'):
        op.drop_index(index, table_name='card_loans')
    op.drop_table('card_loans')
