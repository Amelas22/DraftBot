"""Which team a roster command edits.

The no-`team` path used to take .first() on the captain lookup, so a captain who
owned two teams had their edit land on whichever row came back first, with no
error saying so. These tests pin the resolution rules down.
"""
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
from services.tournament_service import create_tournament, register_team


@pytest_asyncio.fixture
async def test_db():
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()
    os.unlink(temp_db.name)


def _ctx(author_id):
    ctx = MagicMock()
    ctx.author.id = author_id
    ctx.followup.send = AsyncMock()
    return ctx


def _cog():
    from cogs.tournament_commands import TournamentCog
    return TournamentCog.__new__(TournamentCog)


async def _resolve(ctx, session, tournament, team=None, manager=False):
    with patch("cogs.tournament_commands.is_bot_manager",
               new=AsyncMock(return_value=manager)):
        return await _cog()._roster_target(ctx, session, tournament, team)


def _reply(ctx):
    return ctx.followup.send.call_args.args[0]


@pytest.mark.asyncio
async def test_captain_of_one_team_needs_no_team_argument(test_db):
    async with test_db() as session:
        t = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        alpha, _ = await register_team(session, t.id, "Alpha", "42")
        await session.commit()

        ctx = _ctx(42)
        assert (await _resolve(ctx, session, t)).id == alpha.id
        ctx.followup.send.assert_not_called()


@pytest.mark.asyncio
async def test_captain_of_two_teams_is_asked_which_one(test_db):
    """The bug: this used to silently pick Alpha and edit the wrong roster."""
    async with test_db() as session:
        t = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        await register_team(session, t.id, "Alpha", "42")
        await session.commit()
        await register_team(session, t.id, "Bravo", "42")
        await session.commit()

        ctx = _ctx(42)
        assert await _resolve(ctx, session, t) is None
        reply = _reply(ctx)
        assert "Alpha" in reply and "Bravo" in reply and "team" in reply


@pytest.mark.asyncio
async def test_captain_of_nothing_is_told_to_register(test_db):
    async with test_db() as session:
        t = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()

        ctx = _ctx(42)
        assert await _resolve(ctx, session, t) is None
        assert "/tournament register" in _reply(ctx)


@pytest.mark.asyncio
async def test_captain_may_name_their_own_team(test_db):
    """Naming a team you own is how a multi-team captain picks one, so it must not
    require the bot-manager role."""
    async with test_db() as session:
        t = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        await register_team(session, t.id, "Alpha", "42")
        await session.commit()
        bravo, _ = await register_team(session, t.id, "Bravo", "42")
        await session.commit()

        ctx = _ctx(42)
        got = await _resolve(ctx, session, t, team="Bravo", manager=False)
        assert got is not None and got.id == bravo.id


@pytest.mark.asyncio
async def test_non_manager_cannot_name_someone_elses_team(test_db):
    async with test_db() as session:
        t = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        await register_team(session, t.id, "Alpha", "99")
        await session.commit()

        ctx = _ctx(42)
        assert await _resolve(ctx, session, t, team="Alpha", manager=False) is None
        assert "<@99>" in _reply(ctx)


@pytest.mark.asyncio
async def test_manager_may_name_any_team(test_db):
    async with test_db() as session:
        t = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        alpha, _ = await register_team(session, t.id, "Alpha", "99")
        await session.commit()

        ctx = _ctx(42)
        got = await _resolve(ctx, session, t, team="Alpha", manager=True)
        assert got is not None and got.id == alpha.id


@pytest.mark.asyncio
async def test_unknown_team_name_is_reported(test_db):
    async with test_db() as session:
        t = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()

        ctx = _ctx(42)
        assert await _resolve(ctx, session, t, team="Nope", manager=True) is None
        assert "Nope" in _reply(ctx)
