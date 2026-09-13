"""Re-submitting a match result must not double-apply rating updates.

apply_result_report is the live-path entry: first reports apply one
incremental TrueSkill update; same-winner re-reports (score corrections,
a teammate reporting again) do nothing; winner changes heal player_stats
by replaying the match_results ledger from scratch.
"""
import os
import tempfile
from datetime import datetime

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


@pytest.mark.asyncio
async def test_score_fix_to_2_1_ends_the_perfect_streak(test_db):
    """A 2-0 corrected to 2-1 must stop counting as a clean sweep.

    The winner does not change, so the live path takes the 'none' branch and
    nothing re-runs -- but the match is no longer perfect, and the perfect
    streak standing on it is now standing on a score that does not exist.
    """
    from utils import apply_result_report
    match_id = await _seed_match(winner_id="1")

    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, match_id)
    await apply_result_report(mr, previous_winner_id=None)      # 2-0: sweep
    assert (await _stats("1")).current_perfect_streak == 1

    # Correction: it was really 2-1. Same winner, so action is 'none'.
    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, match_id)
        mr.player2_wins = 1
        await session.commit()
    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, match_id)
    await apply_result_report(mr, previous_winner_id="1")

    winner = await _stats("1")
    assert winner.current_perfect_streak == 0
    assert winner.current_perfect_streak_started_at is None


@pytest.mark.asyncio
async def test_winner_flip_moves_the_perfect_streak_to_the_real_winner(test_db):
    """A flipped winner must reset the mis-credited player's perfect streak.

    The wrongly-credited player is now the loser of this match, and a loss
    unconditionally ends a perfect streak.
    """
    from utils import apply_result_report
    match_id = await _seed_match(winner_id="1")

    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, match_id)
    await apply_result_report(mr, previous_winner_id=None)      # 1 swept 2
    assert (await _stats("1")).current_perfect_streak == 1

    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, match_id)
        mr.winner_id = "2"
        mr.player1_wins, mr.player2_wins = 0, 2
        await session.commit()
    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, match_id)
    await apply_result_report(mr, previous_winner_id="1")

    p1, p2 = await _stats("1"), await _stats("2")
    assert p1.current_perfect_streak == 0
    assert p1.current_perfect_streak_started_at is None
    assert p2.current_perfect_streak == 1


async def _seed_series(n):
    """n matches between the same two players, all reported as 2-0 to player 1."""
    ids = []
    async with AsyncSessionLocal() as session:
        session.add(DraftSession(
            session_id="s1", guild_id="g", session_type="staked"))
        for i in range(1, n + 1):
            mr = MatchResult(
                session_id="s1", match_number=i,
                player1_id="1", player2_id="2",
                player1_wins=2, player2_wins=0, winner_id="1")
            session.add(mr)
            ids.append(mr)
        await session.commit()
        return [mr.id for mr in ids]


async def _report(match_id, previous_winner_id):
    from utils import apply_result_report
    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, match_id)
    return await apply_result_report(mr, previous_winner_id)


async def _history_counts(pid):
    from models.win_streak_history import WinStreakHistory
    from models.perfect_streak_history import PerfectStreakHistory
    async with AsyncSessionLocal() as session:
        wins = (await session.execute(select(WinStreakHistory).where(
            WinStreakHistory.player_id == pid))).scalars().all()
        perfects = (await session.execute(select(PerfectStreakHistory).where(
            PerfectStreakHistory.player_id == pid))).scalars().all()
    return len(wins), len(perfects)


@pytest.mark.asyncio
async def test_correcting_a_misreported_loss_restores_the_streak(test_db):
    """A streak broken by a misreport comes back when the report is fixed.

    The whole point of durable corrections: a player who really won three in a
    row should not lose the streak because someone clicked the wrong button and
    then fixed it.
    """
    m1, m2, m3 = await _seed_series(3)
    await _report(m1, None)
    await _report(m2, None)
    assert (await _stats("1")).current_win_streak == 2

    # Match 3 is misreported as a loss for player 1.
    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, m3)
        mr.winner_id = "2"
        mr.player1_wins, mr.player2_wins = 0, 2
        await session.commit()
    await _report(m3, None)

    broken = await _stats("1")
    assert broken.current_win_streak == 0
    assert broken.current_perfect_streak == 0

    # Fixed: player 1 actually swept it.
    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, m3)
        mr.winner_id = "1"
        mr.player1_wins, mr.player2_wins = 2, 0
        await session.commit()
    await _report(m3, previous_winner_id="2")

    healed = await _stats("1")
    assert healed.current_win_streak == 3
    assert healed.current_perfect_streak == 3
    assert healed.longest_win_streak == 3
    assert healed.longest_perfect_streak == 3


@pytest.mark.asyncio
async def test_correction_erases_history_rows_the_misreport_wrote(test_db):
    """Streak history follows the ledger: a streak that never really ended
    must not keep a 'this is how it ended' row from the wrong report."""
    m1, m2, m3 = await _seed_series(3)
    await _report(m1, None)
    await _report(m2, None)

    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, m3)
        mr.winner_id = "2"
        mr.player1_wins, mr.player2_wins = 0, 2
        await session.commit()
    await _report(m3, None)
    assert await _history_counts("1") == (1, 1), "the misreport ended both streaks"

    async with AsyncSessionLocal() as session:
        mr = await session.get(MatchResult, m3)
        mr.winner_id = "1"
        mr.player1_wins, mr.player2_wins = 2, 0
        await session.commit()
    await _report(m3, previous_winner_id="2")

    assert await _history_counts("1") == (0, 0)


@pytest.mark.asyncio
async def test_correcting_an_old_match_does_not_reorder_the_replay(test_db):
    """A correction must not move its match to the end of the player's history.

    views.py stamps result_submitted_at with the correction time on every
    report, so ordering a replay by it puts a corrected match AFTER matches
    that were really played later. Streaks are about the order games were
    played, and that does not change when a score is fixed.

    Played: A beats B, A beats C, then D beats A. A's streak is 0, best 2.
    Correcting the FIRST match must leave both of those alone.
    """
    from helpers.skill import backfill_streaks
    from sqlalchemy import create_engine

    played = datetime(2026, 9, 1, 20, 0)
    async with AsyncSessionLocal() as session:
        session.add(DraftSession(
            session_id="s9", guild_id="g", session_type="staked",
            teams_start_time=played, draft_start_time=played))
        rows = [("A", "B", 2, 0, "A", datetime(2026, 9, 1, 20, 10)),
                ("A", "C", 2, 0, "A", datetime(2026, 9, 1, 20, 20)),
                ("D", "A", 2, 0, "D", datetime(2026, 9, 1, 20, 30))]
        for number, (p1, p2, w1, w2, winner, submitted) in enumerate(rows, 1):
            session.add(MatchResult(
                session_id="s9", match_number=number,
                player1_id=p1, player2_id=p2, player1_wins=w1, player2_wins=w2,
                winner_id=winner, result_submitted_at=submitted))
        await session.commit()

    # The first match is corrected long afterwards; same winner, 2-0 -> 2-1.
    async with AsyncSessionLocal() as session:
        mr = (await session.execute(select(MatchResult).where(
            MatchResult.session_id == "s9", MatchResult.match_number == 1)
        )).scalars().first()
        mr.player2_wins = 1
        mr.result_submitted_at = datetime(2026, 9, 1, 21, 0)
        await session.commit()

    url = AsyncSessionLocal.kw["bind"].url.render_as_string(
        hide_password=False).replace("+aiosqlite", "")
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            backfill_streaks(conn, ["A"], "g")
    finally:
        engine.dispose()

    a = await _stats("A")
    assert a.current_win_streak == 0, "A's last game played was a loss"
    assert a.longest_win_streak == 2, "A really did win two in a row first"
