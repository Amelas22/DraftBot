"""Tests for services/tournament_service.py (Slice 1)."""
import os
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
from models.team import Team
from models.tournament import Tournament, TournamentParticipant
from services.tournament_service import (
    create_tournament,
    get_active_tournament,
    list_participants,
    register_team,
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


# ---- create_tournament / get_active_tournament -------------------------------

@pytest.mark.asyncio
async def test_create_tournament_opens_registration(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
    assert tournament.status == "registration"
    assert tournament.total_rounds == 3
    assert tournament.current_round == 0


@pytest.mark.asyncio
async def test_create_rejects_second_active_tournament_in_guild(test_db):
    async with test_db() as session:
        await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        with pytest.raises(ValueError):
            await create_tournament(session, "g1", "Summer", 3)


@pytest.mark.asyncio
async def test_create_allowed_in_other_guild_and_after_completion(test_db):
    async with test_db() as session:
        first = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()

        # Another guild is independent
        await create_tournament(session, "g2", "Spring", 3)
        await session.commit()

        # Completing the first frees the guild
        first.status = "completed"
        await session.commit()
        await create_tournament(session, "g1", "Summer", 3)
        await session.commit()


@pytest.mark.asyncio
async def test_get_active_tournament(test_db):
    async with test_db() as session:
        assert await get_active_tournament(session, "g1") is None
        created = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()

        active = await get_active_tournament(session, "g1")
        assert active is not None and active.id == created.id
        assert await get_active_tournament(session, "g2") is None

        created.status = "completed"
        await session.commit()
        assert await get_active_tournament(session, "g1") is None


# ---- register_team ------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_team_creates_team_and_participant(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()

        participant, created = await register_team(session, tournament.id, "Alpha", "42")
        await session.commit()

        assert created is True
        assert participant.team_name == "Alpha"
        assert participant.captain_user_id == "42"

        teams = (await session.execute(select(Team))).scalars().all()
        assert len(teams) == 1 and teams[0].TeamName == "Alpha"
        assert participant.team_id == teams[0].TeamID


@pytest.mark.asyncio
async def test_register_team_reuses_existing_team_case_insensitively(test_db):
    async with test_db() as session:
        session.add(Team(TeamName="Alpha"))
        await session.commit()

        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()

        participant, created = await register_team(session, tournament.id, "  alpha ", "42")
        await session.commit()

        assert created is True
        teams = (await session.execute(select(Team))).scalars().all()
        assert len(teams) == 1  # no duplicate team
        assert participant.team_name == "Alpha"  # canonical stored name


@pytest.mark.asyncio
async def test_register_team_is_idempotent(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()

        first, created_first = await register_team(session, tournament.id, "Alpha", "42")
        await session.commit()
        second, created_second = await register_team(session, tournament.id, "Alpha", "99")
        await session.commit()

        assert created_first is True and created_second is False
        assert second.id == first.id
        assert second.captain_user_id == "42"  # original captain kept

        participants = (await session.execute(select(TournamentParticipant))).scalars().all()
        assert len(participants) == 1


@pytest.mark.asyncio
async def test_register_team_rejected_outside_registration(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        tournament.status = "active"
        await session.commit()

        with pytest.raises(ValueError):
            await register_team(session, tournament.id, "Alpha", "42")


@pytest.mark.asyncio
async def test_register_team_rejects_unknown_tournament(test_db):
    async with test_db() as session:
        with pytest.raises(ValueError):
            await register_team(session, 999, "Alpha", "42")


# ---- list_participants ----------------------------------------------------------

@pytest.mark.asyncio
async def test_list_participants_in_registration_order(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()

        for name, captain in (("Bravo", "1"), ("Alpha", "2"), ("Charlie", "3")):
            await register_team(session, tournament.id, name, captain)
            await session.commit()

        participants = await list_participants(session, tournament.id)
        assert [p.team_name for p in participants] == ["Bravo", "Alpha", "Charlie"]
