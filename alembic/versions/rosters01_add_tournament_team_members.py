"""add tournament team rosters

Tournaments recorded only a captain, so a team's other players were nowhere in
the database. tournament_team_members records them, scoped to the participant
rather than to the global Team identity: teams.TeamName is unique across the
whole database with no guild column, so a roster hung off the team row would
merge two guilds that pick the same team name (there are already teams called
"Echo" and "Foxtrot") and would rewrite a finished tournament's roster when the
team re-entered a later one.

Additive and fully reversible. Dropping the legacy team_registration table is a
separate, destructive migration (dropteamreg01) so it can be reviewed and backed
up on its own -- see that file.

Revision ID: rosters01
Revises: lbgroups01
"""
import sqlalchemy as sa
from alembic import op

revision = "rosters01"
down_revision = "lbgroups01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tournament_team_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("participant_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("added_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["participant_id"], ["tournament_participants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("participant_id", "user_id", name="uq_participant_member"),
    )
    op.create_index("ix_tournament_team_members_participant_id",
                    "tournament_team_members", ["participant_id"])
    # Indexed because the "which other teams is this player on?" lookup filters on
    # user_id across the tournament on every add.
    op.create_index("ix_tournament_team_members_user_id",
                    "tournament_team_members", ["user_id"])


def downgrade():
    op.drop_index("ix_tournament_team_members_user_id",
                  table_name="tournament_team_members")
    op.drop_index("ix_tournament_team_members_participant_id",
                  table_name="tournament_team_members")
    op.drop_table("tournament_team_members")
