"""add trophy quiz tables and leaderboard columns"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'troph1quiz00'
down_revision: Union[str, Sequence[str], None] = 'logcapr0recon'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'trophy_quiz_sessions',
        sa.Column('quiz_id', sa.String(length=64), primary_key=True),
        sa.Column('display_id', sa.Integer(), nullable=False),
        sa.Column('guild_id', sa.String(length=64), nullable=False),
        sa.Column('channel_id', sa.String(length=64), nullable=False),
        sa.Column('message_id', sa.String(length=64), nullable=True),
        sa.Column('draft_session_id', sa.String(length=128), nullable=False),
        sa.Column('decks', sa.JSON(), nullable=False),
        sa.Column('posted_by', sa.String(length=64), nullable=False),
        sa.Column('posted_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('total_participants', sa.Integer(), server_default=sa.text('0')),
    )
    op.create_index('ix_trophy_quiz_sessions_guild_display_id', 'trophy_quiz_sessions',
                    ['guild_id', 'display_id'], unique=True)
    op.create_table(
        'trophy_quiz_submissions',
        sa.Column('quiz_id', sa.String(length=64), sa.ForeignKey('trophy_quiz_sessions.quiz_id'), primary_key=True),
        sa.Column('player_id', sa.String(length=64), primary_key=True),
        sa.Column('display_name', sa.String(length=128)),
        sa.Column('guesses', sa.JSON(), nullable=False),
        sa.Column('direction_correct', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('exact_points', sa.JSON(), nullable=False),
        sa.Column('points_earned', sa.Integer(), nullable=False),
        sa.Column('submitted_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
    )
    with op.batch_alter_table('leaderboard_messages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('trophy_quiz_points_view_message_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('trophy_quiz_points_timeframe', sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('leaderboard_messages', schema=None) as batch_op:
        batch_op.drop_column('trophy_quiz_points_timeframe')
        batch_op.drop_column('trophy_quiz_points_view_message_id')
    op.drop_index('ix_trophy_quiz_sessions_guild_display_id', table_name='trophy_quiz_sessions')
    op.drop_table('trophy_quiz_submissions')
    op.drop_table('trophy_quiz_sessions')
