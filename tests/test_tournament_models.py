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
from models.tournament import Tournament, TournamentParticipant


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
