"""The registration core shared by /tournament register, add_teammate and the
Register a team wizard."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.tournament import TournamentParticipant, TournamentTeamMember
from services import tournament_registration as reg
from services import wallet_service
from services.tournament_service import create_tournament, register_team

GUILD = "g1"


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
    """The board refresh talks to Discord; not under test here."""
    return patch.object(reg, "refresh_boards", AsyncMock())


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
