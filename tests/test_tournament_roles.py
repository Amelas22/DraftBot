"""Tournament team roles: naming, creation with rollback, sync, deletion."""
import os
import tempfile
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
from models.tournament import Tournament, TournamentParticipant, TournamentTeamMember
from services.tournament_service import store_role_ids
from services.tournament_roles import (
    create_team_roles,
    delete_team_roles,
    sync_member,
    team_role_name,
)


@pytest_asyncio.fixture
async def test_db():
    """A real aiosqlite-backed async session factory -- not a mock. This is
    what the fixed-round-1 test below needs: SimpleNamespace-based
    _participant() stubs cannot reproduce a lazy-loaded relationship raising
    MissingGreenlet, since they never touch a session at all."""
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


def _participant(pid, team_name, captain, roster=()):
    return SimpleNamespace(id=pid, team_name=team_name,
                           captain_user_id=str(captain),
                           roster_user_ids=[str(u) for u in roster])


def _guild(create_fails_on=None, present_user_ids=()):
    """A guild stand-in. `create_fails_on` is a team name whose role creation
    raises, so the all-or-nothing rollback can be exercised. `present_user_ids`
    lists which user ids are still in the guild: each gets a member stub whose
    `add_roles`/`remove_roles` are `AsyncMock`s, reachable via `guild.members`,
    so a test can assert who actually got which role rather than just a call
    count. Any id not listed resolves to None from `get_member`, matching a
    player who has left the guild -- the default when no ids are given, so
    tests that only care about role creation/deletion don't need to think
    about membership at all."""
    created = []

    async def create_role(name, **kwargs):
        if name == create_fails_on:
            raise discord.Forbidden(MagicMock(status=403), "Missing Permissions")
        role = MagicMock()
        role.id = 1000 + len(created)
        role.name = name
        role.delete = AsyncMock()
        created.append(role)
        return role

    members = {}
    for uid in present_user_ids:
        member = MagicMock()
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        members[int(uid)] = member

    guild = MagicMock()
    guild.create_role = AsyncMock(side_effect=create_role)
    guild.created = created
    guild.members = members
    guild.get_member = MagicMock(side_effect=lambda uid: members.get(uid))
    return guild


def test_role_name_is_the_team_name():
    assert team_role_name("Alpha Squad") == "Alpha Squad"


def test_role_name_is_clipped_to_discords_limit():
    assert len(team_role_name("x" * 250)) == 100


def test_role_name_of_exactly_the_limit_is_untouched():
    name = "y" * 100
    assert team_role_name(name) == name


@pytest.mark.asyncio
async def test_create_team_roles_returns_a_role_per_participant():
    guild = _guild()
    parts = [_participant(1, "Alpha", 10), _participant(2, "Bravo", 20)]
    out = await create_team_roles(guild, parts)
    assert set(out) == {1, 2}
    assert [r.name for r in guild.created] == ["Alpha", "Bravo"]
    # mentionable is the whole point: a role nobody can mention cannot pull
    # anyone into a thread.
    assert guild.create_role.await_args_list[0].kwargs["mentionable"] is True


@pytest.mark.asyncio
async def test_a_failed_creation_deletes_the_roles_already_made():
    """Start is all-or-nothing. The database half of that rollback is free --
    db_session rolls back on any exception -- but Discord has no transaction,
    so roles already created are real until this deletes them."""
    guild = _guild(create_fails_on="Charlie")
    parts = [_participant(1, "Alpha", 10), _participant(2, "Bravo", 20),
             _participant(3, "Charlie", 30)]

    with pytest.raises(discord.Forbidden):
        await create_team_roles(guild, parts)

    assert len(guild.created) == 2
    for role in guild.created:
        role.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_team_roles_gives_the_role_to_captain_and_roster():
    """The point of the feature: the role has to actually reach people, not
    just get created. Asserted against the real add_roles calls -- who got
    which role -- not a call count, since a call count survives mutations
    that assign the wrong people or the wrong role."""
    guild = _guild(present_user_ids=["10", "20", "30"])
    parts = [_participant(1, "Alpha", 10, roster=[20, 30])]

    await create_team_roles(guild, parts)

    role = guild.created[0]
    guild.members[10].add_roles.assert_awaited_once_with(role)
    guild.members[20].add_roles.assert_awaited_once_with(role)
    guild.members[30].add_roles.assert_awaited_once_with(role)


@pytest.mark.asyncio
async def test_create_team_roles_succeeds_when_one_roster_member_left_the_guild():
    """One absent player must not fail a 42-team start: creation still
    succeeds and the members still in the guild still get the role."""
    guild = _guild(present_user_ids=["10"])   # roster member 20 has left
    parts = [_participant(1, "Alpha", 10, roster=[20])]

    out = await create_team_roles(guild, parts)

    role = guild.created[0]
    assert out == {1: str(role.id)}
    guild.members[10].add_roles.assert_awaited_once_with(role)


@pytest.mark.asyncio
async def test_sync_member_add_gives_the_role():
    guild = _guild(present_user_ids=["12345"])
    role = MagicMock()
    guild.get_role = MagicMock(return_value=role)

    await sync_member(guild, "1000", "12345", add=True)

    guild.members[12345].add_roles.assert_awaited_once_with(role)


@pytest.mark.asyncio
async def test_sync_member_remove_takes_the_role():
    """No coverage at all before this: the remove branch is reachable only
    through add=False, and no earlier test exercised it."""
    guild = _guild(present_user_ids=["12345"])
    role = MagicMock()
    guild.get_role = MagicMock(return_value=role)

    await sync_member(guild, "1000", "12345", add=False)

    guild.members[12345].remove_roles.assert_awaited_once_with(role)


@pytest.mark.asyncio
async def test_sync_member_skips_a_member_who_left_the_guild():
    """One absent player must not fail an operation for everyone else."""
    guild = _guild()
    guild.get_member.return_value = None
    await sync_member(guild, "1000", "12345", add=True)   # must not raise


@pytest.mark.asyncio
async def test_delete_team_roles_treats_an_already_deleted_role_as_done():
    """Roles outlive the bot process; cleanup has to be idempotent."""
    gone = MagicMock()
    gone.delete = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "unknown role"))
    live = MagicMock()
    live.delete = AsyncMock()
    guild = MagicMock()
    guild.get_role = MagicMock(side_effect=lambda rid: {1: gone, 2: live}.get(rid))

    assert await delete_team_roles(guild, ["1", "2"]) == 1
    live.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_roster_user_ids_loads_from_a_fresh_session_without_a_greenlet_error(test_db):
    """A regression test for round 1 of review: team_members must be
    lazy="selectin", not the SQLAlchemy default lazy="select".

    This codebase's sessions are all AsyncSession (database/db_session.py).
    A lazy="select" relationship issues its SELECT at attribute-access time,
    which needs a greenlet that async code does not have outside an
    explicit await -- so reading .roster_user_ids on a participant that came
    back from a query would raise MissingGreenlet. That only shows up when
    the participant is loaded through a real session and then read again
    after the loading query's greenlet is gone -- which is why this uses a
    FRESH session to read it back, rather than the session that created the
    rows: reusing that session would leave the rows (and their loaded
    relationship) sitting in the identity map, and the lazy load would never
    actually fire.
    """
    async with test_db() as session:
        tournament = Tournament(guild_id="g1", name="Cup", total_rounds=3)
        session.add(tournament)
        await session.flush()

        participant = TournamentParticipant(
            tournament_id=tournament.id, team_id=1, team_name="Alpha",
            captain_user_id="10",
        )
        session.add(participant)
        await session.flush()

        session.add(TournamentTeamMember(
            participant_id=participant.id, user_id="200", display_name="Roster Player",
        ))
        await session.commit()
        participant_id = participant.id

    async with test_db() as fresh_session:
        result = await fresh_session.execute(
            select(TournamentParticipant).where(TournamentParticipant.id == participant_id)
        )
        fresh_participant = result.scalar_one()
        assert fresh_participant.roster_user_ids == ["200"]


def _route_store_role_ids_at(test_db):
    """store_role_ids (services/tournament_service.py) opens its own session via
    `database.db_session.db_session`, not the raw `test_db` factory the rest of
    this file uses directly -- so testing it for real means patching that name
    to open sessions against this fixture's engine instead of the real one."""
    @asynccontextmanager
    async def fake_db_session():
        async with test_db() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    return patch("services.tournament_service.db_session", fake_db_session)


@pytest.mark.asyncio
async def test_store_role_ids_persists_and_is_readable_from_a_fresh_session(test_db):
    """store_role_ids is Task 3's write half of Task 1's role_id column, and
    tests/test_tournament_cog_smoke.py only ever mocks it -- nothing there
    would catch a wrong attribute name or a write that never got committed.
    Reading back through a FRESH session (rather than the one that wrote it)
    is what actually proves the commit happened, mirroring
    test_roster_user_ids_loads_from_a_fresh_session_without_a_greenlet_error
    above."""
    async with test_db() as session:
        tournament = Tournament(guild_id="g1", name="Cup", total_rounds=3)
        session.add(tournament)
        await session.flush()
        participant = TournamentParticipant(
            tournament_id=tournament.id, team_id=1, team_name="Alpha",
            captain_user_id="10",
        )
        session.add(participant)
        await session.commit()
        participant_id = participant.id

    with _route_store_role_ids_at(test_db):
        await store_role_ids({participant_id: "555"})

    async with test_db() as fresh_session:
        result = await fresh_session.execute(
            select(TournamentParticipant).where(TournamentParticipant.id == participant_id)
        )
        assert result.scalar_one().role_id == "555"


@pytest.mark.asyncio
async def test_store_role_ids_logs_and_skips_a_participant_that_has_vanished(test_db):
    """A team can drop (with a refund) in the gap between _create_roles_for_start's
    read and the money-locked start -- its role was already created and
    assigned, and store_role_ids finds no row to record it against. It must
    not raise (the other, real ids in the same call still need to be saved),
    but the skip must be logged like every other skip in this feature."""
    with _route_store_role_ids_at(test_db), \
         patch("services.tournament_service.logger") as mock_logger:
        await store_role_ids({999999: "555"})  # no such participant

    mock_logger.warning.assert_called_once()
    logged = str(mock_logger.warning.call_args.args[0])
    assert "999999" in logged and "555" in logged
