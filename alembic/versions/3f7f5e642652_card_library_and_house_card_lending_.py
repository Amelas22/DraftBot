"""card library and house card lending share a history

Revision ID: 3f7f5e642652
Revises: cardloan01, e197552c0f87
Create Date: 2026-09-16 19:19:47.533455

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f7f5e642652'
down_revision: Union[str, Sequence[str], None] = ('cardloan01', 'e197552c0f87')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
