"""update_player_stats_and_elo lazily creates a player_stats row for a premade
player who has none yet, instead of silently failing."""
import os
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from database.models_base import Base
from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from models.player import PlayerStats
from models.match import MatchResult


@pytest_asyncio.fixture
async def test_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    AsyncSessionLocal.configure(bind=engine)
    yield engine
    await engine.dispose()
    os.unlink(tmp.name)


@pytest.mark.asyncio
async def test_lazy_creates_missing_rows_and_rates_them(test_db):
    from utils import update_player_stats_and_elo
    async with AsyncSessionLocal() as session:
        session.add(DraftSession(
            session_id="s1", guild_id="g", session_type="premade"))
        # player "1" already has a row; player "2" does NOT
        session.add(PlayerStats(
            player_id="1", guild_id="g", display_name="One",
            true_skill_mu=25.0, true_skill_sigma=8.333,
            games_won=0, games_lost=0, drafts_participated=0,
            current_win_streak=0, longest_win_streak=0,
            current_perfect_streak=0, longest_perfect_streak=0,
            current_draft_win_streak=0, longest_draft_win_streak=0,
            team_drafts_won=0, team_drafts_lost=0, team_drafts_tied=0))
        mr = MatchResult(
            session_id="s1", match_number=1,
            player1_id="1", player2_id="2",
            player1_wins=2, player2_wins=0, winner_id="1")
        session.add(mr)
        await session.commit()
        match_id = mr.id

    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, match_id)

    await update_player_stats_and_elo(mr)

    async with AsyncSessionLocal() as session:
        p2 = (await session.execute(
            select(PlayerStats).where(
                PlayerStats.player_id == "2", PlayerStats.guild_id == "g"))).scalars().first()
    assert p2 is not None                    # row was lazily created
    assert p2.games_lost == 1                 # counted the loss
    assert p2.true_skill_mu < 25.0            # rated below the prior
    assert p2.drafts_participated == 0        # NOT counted as a draft
