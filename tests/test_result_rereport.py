"""Re-submitting a match result must not double-apply rating updates.

apply_result_report is the live-path entry: first reports apply one
incremental TrueSkill update; same-winner re-reports (score corrections,
a teammate reporting again) do nothing; winner changes heal player_stats
by replaying the match_results ledger from scratch.
"""
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


async def _seed_match(winner_id="1"):
    async with AsyncSessionLocal() as session:
        session.add(DraftSession(
            session_id="s1", guild_id="g", session_type="staked"))
        mr = MatchResult(
            session_id="s1", match_number=1,
            player1_id="1", player2_id="2",
            player1_wins=2, player2_wins=0, winner_id=winner_id)
        session.add(mr)
        await session.commit()
        return mr.id


async def _stats(pid):
    async with AsyncSessionLocal() as session:
        return (await session.execute(
            select(PlayerStats).where(
                PlayerStats.player_id == pid, PlayerStats.guild_id == "g")
        )).scalars().first()


@pytest.mark.asyncio
async def test_same_winner_rereport_does_not_double_count(test_db):
    from utils import apply_result_report
    match_id = await _seed_match(winner_id="1")

    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, match_id)
    action, extensions = await apply_result_report(mr, previous_winner_id=None)
    assert action == "apply" and extensions is not None

    first = await _stats("1")
    mu_after_first, games_after_first = first.true_skill_mu, first.games_won

    # Score correction / duplicate report: same winner selected again.
    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, match_id)
    action, extensions = await apply_result_report(mr, previous_winner_id="1")
    assert action == "none" and extensions is None

    again = await _stats("1")
    assert again.games_won == games_after_first
    assert again.true_skill_mu == mu_after_first


@pytest.mark.asyncio
async def test_winner_flip_recomputes_from_ledger(test_db):
    from utils import apply_result_report
    match_id = await _seed_match(winner_id="1")

    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, match_id)
    await apply_result_report(mr, previous_winner_id=None)          # 1 beat 2

    # Correction: player 2 actually won. Flip the stored row, then report.
    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, match_id)
        mr.winner_id = "2"
        mr.player1_wins, mr.player2_wins = 0, 2
        await session.commit()
    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, match_id)
    action, extensions = await apply_result_report(mr, previous_winner_id="1")
    assert action == "recompute" and extensions is None

    # Stats now reflect the ledger as if the wrong report never happened.
    p1, p2 = await _stats("1"), await _stats("2")
    assert (p1.games_won, p1.games_lost) == (0, 1)
    assert (p2.games_won, p2.games_lost) == (1, 0)
    assert p2.true_skill_mu > 25.0
    assert p1.true_skill_mu < 25.0
