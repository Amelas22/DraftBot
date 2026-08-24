"""Tournament team roles: naming, creation with rollback, sync, deletion."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from services.tournament_roles import (
    create_team_roles,
    delete_team_roles,
    sync_member,
    team_role_name,
)


def _participant(pid, team_name, captain, roster=()):
    return SimpleNamespace(id=pid, team_name=team_name,
                           captain_user_id=str(captain),
                           roster_user_ids=[str(u) for u in roster])


def _guild(create_fails_on=None):
    """A guild stand-in. `create_fails_on` is a team name whose role creation
    raises, so the all-or-nothing rollback can be exercised."""
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

    guild = MagicMock()
    guild.create_role = AsyncMock(side_effect=create_role)
    guild.created = created
    guild.get_member = MagicMock(return_value=None)
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
