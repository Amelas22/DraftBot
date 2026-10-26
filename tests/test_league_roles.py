"""League roles: the live-member lookup and the League Participant / Active grants."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger

import config as config_module
from database.db_session import db_session
from services.league_roles import (
    active_role,
    grant_league_roles,
    league_participant_role,
    live_member_ids,
    release_league_role,
)
from services.tournament_service import add_teammate, create_tournament, register_team

GUILD = "g1"


@pytest.fixture(autouse=True)
def _default_config():
    """Role names from the defaults, without writing a configs/<guild>.json."""
    cfg = {"roles": {"league_participant": "League Participant"},
           "activity_tracking": {"active_role": "Active"}}
    with patch("services.league_roles.get_config", return_value=cfg):
        yield


async def _team(session, tournament, name, captain, roster=()):
    participant, _ = await register_team(session, tournament.id, name, captain)
    for uid in roster:
        await add_teammate(session, participant, uid, f"user{uid}")
    return participant


@pytest.mark.asyncio
async def test_live_members_are_captains_and_rosters_of_running_and_registering_tournaments(test_db):
    async with db_session() as session:
        reg = await create_tournament(session, GUILD, "Reg", 3)
        await _team(session, reg, "Alpha", "10", roster=["11"])
        running = await create_tournament(session, "g2", "Run", 3)
        await _team(session, running, "Bravo", "20")
        running.status = "active"
        done = await create_tournament(session, "g3", "Done", 3)
        await _team(session, done, "Charlie", "30")
        done.status = "completed"
        other = await create_tournament(session, "g4", "Other", 3)
        await _team(session, other, "Delta", "40")

    async with db_session() as session:
        assert await live_member_ids(session, GUILD) == {"10", "11"}
        assert await live_member_ids(session, "g2") == {"20"}
        assert await live_member_ids(session, "g3") == set()
        assert await live_member_ids(session, "g4") == {"40"}


@pytest.mark.asyncio
async def test_a_dropped_team_is_not_live(test_db):
    async with db_session() as session:
        t = await create_tournament(session, GUILD, "Reg", 3)
        await _team(session, t, "Alpha", "10", roster=["11"])
        bravo = await _team(session, t, "Bravo", "20")
        bravo.dropped_at = datetime.now()

    async with db_session() as session:
        assert await live_member_ids(session, GUILD) == {"10", "11"}


@pytest.mark.asyncio
async def test_a_pending_team_is_live(test_db):
    async with db_session() as session:
        t = await create_tournament(session, GUILD, "Reg", 3)
        alpha = await _team(session, t, "Alpha", "10")
        alpha.status = "pending"

    async with db_session() as session:
        assert await live_member_ids(session, GUILD) == {"10"}


def _role(name):
    role = SimpleNamespace(name=name)
    return role


def _member(*roles):
    member = MagicMock()
    member.roles = list(roles)
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


def _guild(role_names, members):
    guild = MagicMock()
    guild.id = GUILD
    guild.roles = [_role(n) for n in role_names]
    guild.create_role = AsyncMock()
    guild.get_member = MagicMock(side_effect=lambda uid: members.get(uid))
    return guild


def _role_of(guild, name):
    return next(r for r in guild.roles if r.name == name)


@pytest.mark.asyncio
async def test_grant_adds_both_roles_only_where_missing():
    guild = _guild(["League Participant", "Active"], {})
    active = _role_of(guild, "Active")
    holder = _member(active)
    fresh = _member()
    guild.get_member.side_effect = {1: holder, 2: fresh}.get

    assert await grant_league_roles(guild, ["1", "2"]) is True

    holder.add_roles.assert_awaited_once_with(_role_of(guild, "League Participant"))
    given = [a for c in fresh.add_roles.await_args_list for a in c.args]
    assert sorted(r.name for r in given) == ["Active", "League Participant"]


@pytest.mark.asyncio
async def test_grant_for_a_user_not_in_the_guild_is_a_failure():
    guild = _guild(["League Participant", "Active"], {})
    assert await grant_league_roles(guild, ["9"]) is False


@pytest.mark.asyncio
async def test_a_missing_league_role_is_skipped_with_a_warning():
    guild = _guild(["Active"], {})
    member = _member()
    guild.get_member.side_effect = {1: member}.get
    messages = []
    sink = logger.add(messages.append, level="WARNING")
    try:
        assert await grant_league_roles(guild, ["1"]) is True
    finally:
        logger.remove(sink)

    added = [a for c in member.add_roles.await_args_list for a in c.args]
    assert added == [_role_of(guild, "Active")]
    guild.create_role.assert_not_called()
    assert any("League Participant" in str(m) for m in messages)


@pytest.mark.asyncio
async def test_release_keeps_a_user_who_is_still_live(test_db):
    async with db_session() as session:
        t = await create_tournament(session, GUILD, "Reg", 3)
        await _team(session, t, "Alpha", "10")

    guild = _guild(["League Participant", "Active"], {})
    lp = _role_of(guild, "League Participant")
    live, gone = _member(lp), _member(lp)
    guild.get_member.side_effect = {10: live, 20: gone}.get

    assert await release_league_role(guild, ["10", "20"]) is True

    live.remove_roles.assert_not_called()
    gone.remove_roles.assert_awaited_once_with(lp)


@pytest.mark.asyncio
async def test_release_removes_a_user_who_is_live_nowhere(test_db):
    guild = _guild(["League Participant", "Active"], {})
    lp = _role_of(guild, "League Participant")
    member = _member(lp)
    guild.get_member.side_effect = {20: member}.get

    assert await release_league_role(guild, ["20", "21"]) is True  # 21 has left

    member.remove_roles.assert_awaited_once_with(lp)
    # Active is never removed by this feature.
    assert all(_role_of(guild, "Active") not in c.args for c in member.remove_roles.await_args_list)


def test_get_config_backfills_league_participant():
    guild_id = "999000999000999001"
    config_module.bot_config.configs[guild_id] = {"roles": {"admin": "Cube Overseer"}}
    try:
        with patch.object(config_module.bot_config, "save_config"):
            config_module.migrate_configs()
        assert config_module.get_config(guild_id)["roles"]["league_participant"] == "League Participant"
    finally:
        del config_module.bot_config.configs[guild_id]


@pytest.mark.asyncio
async def test_inactivity_sweep_skips_live_tournament_players(test_db):
    from datetime import timedelta
    from session import AsyncSessionLocal, PlayerStats
    from utils import _sweep_guild

    old = datetime.now() - timedelta(days=400)
    async with db_session() as session:
        t = await create_tournament(session, GUILD, "Reg", 3)
        await _team(session, t, "Alpha", "10")
    async with AsyncSessionLocal() as session:
        session.add_all([
            PlayerStats(player_id=pid, guild_id=GUILD, display_name=pid, last_draft_timestamp=old)
            for pid in ("10", "20")
        ])
        await session.commit()

    guild = _guild(["Active", "degen"], {})
    guild.name = "Guild"
    mod_chat = MagicMock()
    mod_chat.name = "mod-chat"
    mod_chat.send = AsyncMock()
    guild.text_channels = [mod_chat]
    active = _role_of(guild, "Active")
    live, idle = _member(active), _member(active)
    live.display_name, idle.display_name = "Live", "Idle"
    guild.get_member.side_effect = {10: live, 20: idle}.get

    cfg = {"activity_tracking": {"enabled": True, "active_role": "Active"}}
    messages = []
    sink = logger.add(messages.append, level="INFO")
    try:
        with patch("config.get_config", return_value=cfg):
            removed = await _sweep_guild(MagicMock(), guild)
    finally:
        logger.remove(sink)

    live.remove_roles.assert_not_called()
    idle.remove_roles.assert_awaited_once_with(active)
    assert removed == ["Idle"]
    assert any("Skipping Live: in the current tournament" in str(m) for m in messages)
