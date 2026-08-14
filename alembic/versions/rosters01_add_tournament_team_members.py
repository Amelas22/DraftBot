"""add tournament team rosters, drop the legacy team_registration table

Tournaments recorded only a captain, so a team's other players were nowhere in
the database. tournament_team_members records them, scoped to the participant
rather than to the team identity: teams.TeamName is unique across the whole
database with no guild column, so a roster hung off the team row would merge two
guilds that pick the same team name (there are already teams called "Echo" and
"Foxtrot") and would rewrite a finished tournament's roster when the team
re-entered a later one.

team_registration is the old league's version of the same idea, keyed 1:1 to that
globally-unique team name. Its last reader went away with league.py in 81318df
("league cleanup"); the model has since been imported but never used. It is
dropped here rather than left as schema drift.

NOTE: dropping it destroys its rows, and production runs `alembic upgrade head`
from the systemd unit with no backup step in front of it. Its 14 rows (53 league
players) were dumped before this migration was written. They are legacy
special-guild league rosters and share no team with any tournament participant --
team_registration covers TeamID 1-14, every tournament team is 15 and up -- so
nothing here can be recovered into the new table anyway.

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
    # Indexed because the "is this player already on another team?" check filters
    # on user_id across the tournament on every add.
    op.create_index("ix_tournament_team_members_user_id",
                    "tournament_team_members", ["user_id"])

    op.drop_table("team_registration")


def downgrade():
    # Recreates team_registration's shape only. Its rows are not restorable here;
    # see the dump referenced in this migration's docstring.
    op.create_table(
        "team_registration",
        sa.Column("ID", sa.Integer(), nullable=False),
        sa.Column("TeamID", sa.Integer(), nullable=True),
        sa.Column("TeamName", sa.String(length=128), nullable=False),
        sa.Column("TeamMembers", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("ID"),
        sa.UniqueConstraint("TeamName"),
    )
    op.drop_index("ix_tournament_team_members_user_id",
                  table_name="tournament_team_members")
    op.drop_index("ix_tournament_team_members_participant_id",
                  table_name="tournament_team_members")
    op.drop_table("tournament_team_members")
