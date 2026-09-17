"""Move library custody out of per-guild scope

Revision ID: b7c2e1a04d31
Revises: 3f7f5e642652
Create Date: 2026-09-17

Cards a player deposited were booked under the server the trade was started in,
but the library is one MTGO account and the cards sit on one shelf. The effect
was that a deposit made in one server was invisible in another, and a withdrawal
started in the second booked its returns against the second server's claim:
that server went negative, the first still claimed the cards, and the shelf was
empty -- three different answers to where the cards were.

Custody now books under a reserved scope instead of a guild id. This repoints
the rows already written. It touches only rows facing `house:library`, which is
the custody counterparty and is used for nothing else; loans face `house:mtgo`
and stay per-server, because a loan really does belong to the server it was
taken out in.

One-way. The per-guild scope is exactly the information this discards, so no
downgrade can reconstruct it, and guessing a guild id would quietly file another
server's custody in the wrong place. downgrade() says so rather than pretending.
The schema does not change, so nothing needs undoing to run older code -- only
this data would have to be put back, and that is a restore, not a migration.
"""
from alembic import op
import sqlalchemy as sa

revision = 'b7c2e1a04d31'
down_revision = '3f7f5e642652'
branch_labels = None
depends_on = None

HOUSE_LIBRARY = "house:library"
LIBRARY_SCOPE = "library"


def upgrade():
    conn = op.get_bind()
    moved = conn.execute(
        sa.text(
            "UPDATE debt_ledger SET guild_id = :scope "
            "WHERE (player_id = :house OR counterparty_id = :house) "
            "AND guild_id != :scope"
        ),
        {"scope": LIBRARY_SCOPE, "house": HOUSE_LIBRARY},
    ).rowcount
    print(f"library custody: repointed {moved} ledger row(s) to '{LIBRARY_SCOPE}'")


def downgrade():
    raise NotImplementedError(
        "Library custody cannot be re-scoped to a guild: which server each row "
        "came from is the information this migration discards, and picking one "
        "would file some other server's custody in the wrong place. The schema "
        "is unchanged, so older code runs against these rows as they are -- it "
        "will simply see no custody, because it looks for it under a guild id. "
        "To genuinely go back, restore the database from a backup taken before "
        "the upgrade."
    )
