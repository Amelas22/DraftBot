"""Tests for the Tournament and TournamentParticipant models (Slice 1)."""
import os
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
from models.tournament import (
    Tournament,
    TournamentMatch,
    TournamentParticipant,
    TournamentRound,
)


@pytest_asyncio.fixture
async def test_db():
    """Create a temporary test database and return a test session factory."""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_session_factory = sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession
    )

    yield test_session_factory

    await engine.dispose()
    os.unlink(temp_db.name)


@pytest.mark.asyncio
async def test_tournament_defaults(test_db):
    async with test_db() as session:
        tournament = Tournament(guild_id="123", name="Spring", total_rounds=3)
        session.add(tournament)
        await session.commit()

        result = await session.execute(select(Tournament))
        saved = result.scalars().one()
        assert saved.status == "registration"
        assert saved.current_round == 0
        assert saved.total_rounds == 3
        assert saved.guild_id == "123"


@pytest.mark.asyncio
async def test_participant_unique_per_tournament_and_team(test_db):
    async with test_db() as session:
        tournament = Tournament(guild_id="123", name="Spring", total_rounds=3)
        session.add(tournament)
        await session.flush()

        session.add(TournamentParticipant(
            tournament_id=tournament.id, team_id=1,
            team_name="Alpha", captain_user_id="42",
        ))
        await session.commit()

    async with test_db() as session:
        session.add(TournamentParticipant(
            tournament_id=1, team_id=1,
            team_name="Alpha again", captain_user_id="43",
        ))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_participant_stat_defaults_are_zero(test_db):
    async with test_db() as session:
        tournament = Tournament(guild_id="123", name="Spring", total_rounds=3)
        session.add(tournament)
        await session.flush()
        session.add(TournamentParticipant(
            tournament_id=tournament.id, team_id=1,
            team_name="Alpha", captain_user_id="42",
        ))
        await session.commit()

        saved = (await session.execute(select(TournamentParticipant))).scalars().one()
        assert (saved.match_wins, saved.match_losses, saved.match_draws) == (0, 0, 0)
        assert (saved.points, saved.game_wins, saved.game_losses, saved.byes) == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_round_number_unique_per_tournament(test_db):
    async with test_db() as session:
        tournament = Tournament(guild_id="123", name="Spring", total_rounds=3)
        session.add(tournament)
        await session.flush()
        session.add(TournamentRound(tournament_id=tournament.id, round_number=1))
        await session.commit()

    async with test_db() as session:
        session.add(TournamentRound(tournament_id=1, round_number=1))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_match_defaults_unreported_and_not_bye(test_db):
    async with test_db() as session:
        tournament = Tournament(guild_id="123", name="Spring", total_rounds=3)
        session.add(tournament)
        await session.flush()
        round_one = TournamentRound(tournament_id=tournament.id, round_number=1)
        session.add(round_one)
        await session.flush()
        session.add(TournamentMatch(
            round_id=round_one.id, team_a_participant_id=1, team_b_participant_id=2,
        ))
        await session.commit()

        match = (await session.execute(select(TournamentMatch))).scalars().one()
        assert match.team_a_wins is None and match.team_b_wins is None
        assert match.is_bye is False
        # per-match Play-button message location + match thread (slice: match threads)
        assert match.pairings_message_id is None
        assert match.pairings_channel_id is None
        assert match.thread_id is None


@pytest.mark.asyncio
async def test_same_team_can_join_different_tournaments(test_db):
    async with test_db() as session:
        for guild in ("g1", "g2"):
            tournament = Tournament(guild_id=guild, name="T", total_rounds=3)
            session.add(tournament)
            await session.flush()
            session.add(TournamentParticipant(
                tournament_id=tournament.id, team_id=1,
                team_name="Alpha", captain_user_id="42",
            ))
        await session.commit()

        result = await session.execute(select(TournamentParticipant))
        assert len(result.scalars().all()) == 2


def test_match_control_message_id_defaults_to_none():
    from models.tournament import TournamentMatch

    match = TournamentMatch(round_id=1, team_a_participant_id=1, team_b_participant_id=2)
    assert match.control_message_id is None


@pytest.mark.asyncio
async def test_second_draft_for_the_same_tournament_match_is_rejected(test_db):
    """DB-level backstop for the premade creation guard (Task 5b fix round 1).

    The guard in sessions/premade_session.py is a check-then-act race: two
    submits milliseconds apart can both pass its query. The invariant it's
    protecting -- at most one draft session per tournament match -- has to
    also hold as a hard constraint, via the unique index on
    draft_sessions.tournament_match_id, regardless of which code path writes
    the column.
    """
    from models.draft_session import DraftSession

    async with test_db() as session:
        session.add(DraftSession(session_id="d1", guild_id="g1", tournament_match_id=77))
        await session.commit()

    async with test_db() as session:
        session.add(DraftSession(session_id="d2", guild_id="g1", tournament_match_id=77))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_unlinked_draft_sessions_can_share_a_null_tournament_match_id(test_db):
    """SQLite permits multiple NULLs in a unique index, so ordinary
    (non-tournament) drafts -- tournament_match_id always None -- are
    unaffected by the guard's unique index."""
    from models.draft_session import DraftSession

    async with test_db() as session:
        session.add(DraftSession(session_id="d3", guild_id="g1"))
        session.add(DraftSession(session_id="d4", guild_id="g1"))
        await session.commit()

        result = await session.execute(select(DraftSession))
        assert len(result.scalars().all()) == 2
