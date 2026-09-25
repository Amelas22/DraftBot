"""card substitutions: what MTGO calls a card the cube calls something else

A Universes Beyond card is listed by CubeCobra under its crossover name and
traded by MTGO under an in-universe one. The library books custody BY NAME, so
a deposit recorded under the cube's name is custody the serve will refuse to
hand back -- `409 asked for 1x 'Norman Osborn' but only 0 held`.

The serve reports each substitution it made on the finished job, so this table
is learned from trades that happened rather than loaded from a card database.
It starts empty and fills itself: the only names worth translating are the ones
the library holds, and it holds only what it has been given.

Revision ID: cardlib03
Revises: cardlib02
"""
import sqlalchemy as sa
from alembic import op

revision = 'cardlib03'
down_revision = 'cardlib02'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'card_substitution',
        # The name the cube list used, and therefore what gets translated.
        sa.Column('cube_name', sa.String(128), primary_key=True),
        # What the serve moved, and what custody is booked under.
        sa.Column('mtgo_name', sa.String(128), nullable=False),
        sa.Column('mtgo_cat_id', sa.Integer(), nullable=True),
        sa.Column('learned_from_job', sa.String(64), nullable=True),
        sa.Column('first_seen', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('card_substitution')
