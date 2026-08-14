"""drop the legacy team_registration table

team_registration was the old league's roster: a {user_id: display_name} JSON map
keyed 1:1 to teams.TeamName, which is unique across the whole database with no
guild column. Its last reader went away with league.py in 81318df ("league
cleanup"); the model has since been imported and never called. Tournament rosters
(tournament_team_members, added in rosters01) replace it with a per-tournament
scope, so this is dead schema rather than something to migrate forward.

DESTRUCTIVE, AND PRODUCTION RUNS MIGRATIONS UNGUARDED. draftbot.service runs
`alembic upgrade head` from ExecStartPre with no backup step, so these rows are
gone the moment the service restarts, and downgrade() below restores the schema
only -- never the data. Its 14 rows (53 league players) were dumped to a file
before this was written. They cover TeamID 1-14 while every tournament team is 15
and up, so none of it could have been carried into the new table anyway.

Kept separate from rosters01 on purpose: that migration is additive and cleanly
reversible, this one is neither, and bundling them would force the drop to ship in
lockstep with the roster feature and make reverting the feature resurrect an
orphan table.

Revision ID: dropteamreg01
Revises: rosters01
"""
import sqlalchemy as sa
from alembic import op

revision = "dropteamreg01"
down_revision = "rosters01"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("team_registration")


def downgrade():
    # Shape only. The rows are not restorable from here; see the dump referenced
    # in this migration's docstring.
    op.create_table(
        "team_registration",
        sa.Column("ID", sa.Integer(), nullable=False),
        sa.Column("TeamID", sa.Integer(), nullable=True),
        sa.Column("TeamName", sa.String(length=128), nullable=False),
        sa.Column("TeamMembers", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("ID"),
        sa.UniqueConstraint("TeamName"),
    )
