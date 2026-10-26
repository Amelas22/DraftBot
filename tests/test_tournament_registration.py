"""The registration core shared by /tournament register, add_teammate and the
Register a team wizard."""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.tournament import TournamentParticipant, TournamentTeamMember
from services import tournament_registration as reg
from services.league_roles import grant_league_roles
from services import wallet_service
from services.tournament_service import create_tournament, register_team

GUILD = "1234"


async def _tournament(fee=0, status="registration"):
    async with db_session() as session:
        t = await create_tournament(session, GUILD, "Lotus Open", 3, entry_fee=fee)
        t.status = status
        return t.id


async def _participant(t_id, name="Alpha", captain="10"):
    async with db_session() as session:
        p, _ = await register_team(session, t_id, name, captain)
        return p.id


def _quiet():
    """The board refresh and the role work talk to Discord; not under test here
    unless a test patches them itself, inside this."""
    stack = ExitStack()
    stack.enter_context(patch.object(reg, "refresh_boards", AsyncMock()))
    stack.enter_context(patch.object(reg, "ensure_team_role", AsyncMock(return_value="1")))
    stack.enter_context(patch.object(reg, "grant_league_roles", AsyncMock(return_value=True)))
    return stack


@pytest.mark.asyncio
async def test_a_free_team_registers_once(test_db):  # noqa: F811
    t_id = await _tournament()
    with _quiet():
        first = await reg.register_captain(MagicMock(), GUILD, t_id, "10", "Alpha")
        again = await reg.register_captain(MagicMock(), GUILD, t_id, "10", "Alpha")

    assert first.kind == "free_registered"
    assert (first.team_name, first.tournament_name) == ("Alpha", "Lotus Open")
    assert again.kind == "free_exists"
    assert again.captain_id == "10"


@pytest.mark.asyncio
async def test_a_paid_team_with_the_tix_is_paid(test_db):  # noqa: F811
    t_id = await _tournament(fee=20)
    await wallet_service.adjust(GUILD, "10", 35, "seed", "test")
    with _quiet():
        out = await reg.register_captain(MagicMock(), GUILD, t_id, "10", "Alpha")

    assert out.kind == "paid"
    assert out.fee == 20
    assert await wallet_service.get_balance(GUILD, "10") == 15


@pytest.mark.asyncio
async def test_a_short_wallet_holds_the_spot_and_names_the_deficit(test_db):  # noqa: F811
    t_id = await _tournament(fee=20)
    await wallet_service.adjust(GUILD, "10", 12, "seed", "test")
    with _quiet():
        out = await reg.register_captain(MagicMock(), GUILD, t_id, "10", "Alpha")

    assert out.kind == "pending"
    assert (out.deficit, out.available) == (8, 12)
    async with db_session() as session:
        p = await session.get(TournamentParticipant, out.participant_id)
        assert p.status != "paid"


@pytest.mark.asyncio
async def test_someone_else_naming_a_pending_team_is_not_its_captain(test_db):  # noqa: F811
    t_id = await _tournament(fee=20)
    with _quiet():
        await reg.register_captain(MagicMock(), GUILD, t_id, "10", "Alpha")
        out = await reg.register_captain(MagicMock(), GUILD, t_id, "11", "Alpha")

    assert out.kind == "not_captain"
    assert out.captain_id == "10"


@pytest.mark.asyncio
async def test_a_closed_tournament_is_refused_with_the_reason(test_db):  # noqa: F811
    t_id = await _tournament(status="active")
    with _quiet():
        out = await reg.register_captain(MagicMock(), GUILD, t_id, "10", "Alpha")

    assert out.kind == "refused"
    assert "registration is closed" in out.error


def _member(user_id, name):
    m = MagicMock()
    m.id = user_id
    m.bot = False
    m.display_name = name
    return m


@pytest.mark.asyncio
async def test_adding_members_puts_them_on_the_roster_and_refreshes_once(test_db):  # noqa: F811
    t_id = await _tournament()
    p_id = await _participant(t_id)
    other = await _participant(t_id, "Bravo", "30")
    async with db_session() as session:
        session.add(TournamentTeamMember(participant_id=other, user_id="21", display_name="Ben"))
    guild = MagicMock()
    guild.get_member.return_value = None
    refresh = AsyncMock()
    with patch.object(reg, "refresh_boards", refresh), \
            patch.object(reg, "ensure_team_role", AsyncMock(return_value="555")), \
            patch.object(reg, "grant_league_roles", AsyncMock(return_value=True)), \
            patch.object(reg, "sync_member", AsyncMock(return_value=True)) as sync:
        adds = await reg.add_roster_members(
            MagicMock(), guild, p_id, [_member(20, "Wren"), _member(21, "Ben")])
        again = await reg.add_roster_members(MagicMock(), guild, p_id, [_member(20, "Wren")])

    assert [(a.user_id, a.created, a.role_synced) for a in adds] == [
        ("20", True, True), ("21", True, True)]
    assert adds[1].also_on == ["Bravo"]
    assert again[0].created is False
    assert refresh.await_count == 2  # once per call, not per member
    assert sync.await_count == 3
    async with db_session() as session:
        rows = (await session.execute(select(TournamentTeamMember.user_id).where(
            TournamentTeamMember.participant_id == p_id))).scalars().all()
    assert sorted(rows) == ["20", "21"]


@pytest.mark.asyncio
async def test_the_captain_cannot_be_added_to_their_own_roster(test_db):  # noqa: F811
    t_id = await _tournament()
    p_id = await _participant(t_id)
    with patch.object(reg, "refresh_boards", AsyncMock()), \
            patch.object(reg, "sync_member", AsyncMock(return_value=True)), \
            pytest.raises(ValueError):
        await reg.add_roster_members(MagicMock(), MagicMock(), p_id,
                                     [_member(20, "Wren"), _member(10, "Rook")])

    async with db_session() as session:
        rows = (await session.execute(select(TournamentTeamMember).where(
            TournamentTeamMember.participant_id == p_id))).scalars().all()
    assert rows == []


def _bot_with(guild):
    bot = MagicMock()
    bot.get_guild.return_value = guild
    return bot


def _role_guild(user_ids, role_names=("League Participant", "Active")):
    """A guild whose members `user_ids` can take the league roles; the team role
    is created through `create_role`."""
    members = {}
    for uid in user_ids:
        m = MagicMock()
        m.roles = []
        m.add_roles = AsyncMock()
        members[int(uid)] = m
    guild = MagicMock()
    guild.roles = [SimpleNamespace(name=n) for n in role_names]
    guild.members = members
    guild.get_member = MagicMock(side_effect=members.get)
    return guild


def _league_config():
    """The real grant_league_roles (undoing _quiet's stub) with default role names."""
    cfg = {"roles": {"league_participant": "League Participant"},
           "activity_tracking": {"active_role": "Active"}}
    stack = ExitStack()
    stack.enter_context(patch("services.league_roles.get_config", return_value=cfg))
    stack.enter_context(patch.object(reg, "grant_league_roles", grant_league_roles))
    return stack


@pytest.mark.asyncio
async def test_registering_creates_the_team_role_and_gives_league_roles(test_db):  # noqa: F811
    t_id = await _tournament()
    guild = _role_guild([10])
    with _quiet(), _league_config(), \
            patch.object(reg, "ensure_team_role", AsyncMock(return_value="555")) as ensure:
        out = await reg.register_captain(_bot_with(guild), GUILD, t_id, "10", "Alpha")

    assert out.kind == "free_registered"
    assert out.roles_synced is True
    ensure.assert_awaited_once_with(guild, out.participant_id)
    given = sorted(a.name for c in guild.members[10].add_roles.await_args_list for a in c.args)
    assert given == ["Active", "League Participant"]


@pytest.mark.asyncio
async def test_a_pending_registration_also_gets_its_roles(test_db):  # noqa: F811
    t_id = await _tournament(fee=20)
    await wallet_service.adjust(GUILD, "10", 12, "seed", "test")
    guild = _role_guild([10])
    with _quiet(), _league_config(), \
            patch.object(reg, "ensure_team_role", AsyncMock(return_value="555")) as ensure:
        out = await reg.register_captain(_bot_with(guild), GUILD, t_id, "10", "Alpha")

    assert out.kind == "pending"
    ensure.assert_awaited_once_with(guild, out.participant_id)
    assert guild.members[10].add_roles.await_count == 1


@pytest.mark.asyncio
async def test_not_captain_gets_no_roles(test_db):  # noqa: F811
    t_id = await _tournament(fee=20)
    guild = _role_guild([10, 11])
    with _quiet(), _league_config(), \
            patch.object(reg, "ensure_team_role", AsyncMock(return_value="555")) as ensure:
        await reg.register_captain(_bot_with(guild), GUILD, t_id, "10", "Alpha")
        ensure.reset_mock()
        guild.members[11].add_roles.reset_mock()
        out = await reg.register_captain(_bot_with(guild), GUILD, t_id, "11", "Alpha")

    assert out.kind == "not_captain"
    ensure.assert_not_awaited()
    guild.members[11].add_roles.assert_not_awaited()


@pytest.mark.asyncio
async def test_sign_up_without_league_role_still_gives_team_role(test_db):  # noqa: F811
    t_id = await _tournament()
    guild = _role_guild([10], role_names=("Active",))
    with _quiet(), _league_config(), \
            patch.object(reg, "ensure_team_role", AsyncMock(return_value="555")) as ensure:
        out = await reg.register_captain(_bot_with(guild), GUILD, t_id, "10", "Alpha")

    ensure.assert_awaited_once()
    assert out.roles_synced is True
    given = [a.name for c in guild.members[10].add_roles.await_args_list for a in c.args]
    assert given == ["Active"]


@pytest.mark.asyncio
async def test_a_refused_team_role_is_reported_not_raised(test_db):  # noqa: F811
    t_id = await _tournament()
    guild = _role_guild([10])
    with _quiet(), _league_config(), \
            patch.object(reg, "ensure_team_role", AsyncMock(return_value=None)):
        out = await reg.register_captain(_bot_with(guild), GUILD, t_id, "10", "Alpha")

    assert out.kind == "free_registered"
    assert out.roles_synced is False


@pytest.mark.asyncio
async def test_adding_members_gives_team_and_league_roles(test_db):  # noqa: F811
    t_id = await _tournament()
    p_id = await _participant(t_id)
    guild = MagicMock()
    with _quiet(), \
            patch.object(reg, "ensure_team_role", AsyncMock(return_value="555")) as ensure, \
            patch.object(reg, "sync_member", AsyncMock(return_value=True)) as sync, \
            patch.object(reg, "grant_league_roles", AsyncMock(side_effect=[True, False])) as grant:
        adds = await reg.add_roster_members(
            MagicMock(), guild, p_id, [_member(20, "Wren"), _member(21, "Ben")])

    ensure.assert_awaited_once_with(guild, p_id)
    assert [c.args for c in sync.await_args_list] == [
        (guild, "555", "20"), (guild, "555", "21")]
    assert [c.args for c in grant.await_args_list] == [(guild, ["20"]), (guild, ["21"])]
    assert [(a.user_id, a.role_synced) for a in adds] == [("20", True), ("21", False)]



def _named_config(active="Active", admin="Cube Overseer"):
    cfg = {"roles": {"league_participant": "League Participant", "admin": admin},
           "activity_tracking": {"active_role": active}}
    stack = ExitStack()
    stack.enter_context(patch("services.league_roles.get_config", return_value=cfg))
    stack.enter_context(patch("services.tournament_roles.get_config", return_value=cfg))
    stack.enter_context(patch("helpers.permissions.get_config", return_value=cfg))
    return stack


async def _team_count(t_id):
    async with db_session() as session:
        return len((await session.execute(select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == t_id))).scalars().all())


def _with_role_ids(guild, ids):
    """Give the guild's roles ids, by name."""
    guild.roles = [SimpleNamespace(id=ids.get(r.name, 0), name=r.name) for r in guild.roles]
    return guild


@pytest.mark.asyncio
async def test_a_pending_non_ascii_team_can_still_pay(test_db):  # noqa: F811
    t_id = await _tournament(fee=20)
    p_id = await _participant(t_id, "Équipe", "10")
    async with db_session() as session:
        (await session.get(TournamentParticipant, p_id)).role_id = "77"
    await wallet_service.adjust(GUILD, "10", 35, "seed", "test")
    guild = _with_role_ids(_role_guild([10], role_names=("Équipe",)), {"Équipe": 77})
    guild.id = GUILD
    with _quiet(), _named_config():
        out = await reg.register_captain(_bot_with(guild), GUILD, t_id, "10", "Équipe")

    assert out.kind == "paid"
    assert out.participant_id == p_id


@pytest.mark.asyncio
async def test_a_team_named_after_a_role_registers_under_a_prefixed_role(test_db):  # noqa: F811
    t_id = await _tournament()
    guild = _role_guild([10], role_names=("League Participant", "Active", "Bot Manager"))
    guild.id = GUILD
    with _quiet(), _named_config(), \
            patch.object(reg, "ensure_team_role", AsyncMock(return_value="555")) as ensure:
        out = await reg.register_captain(_bot_with(guild), GUILD, t_id, "10", "Bot Manager")

    assert out.kind == "free_registered"
    assert await _team_count(t_id) == 1
    ensure.assert_awaited_once()

