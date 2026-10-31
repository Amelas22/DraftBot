"""Commands that pair rounds or open match rooms refuse while a team has no role."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import func, select

from cogs.tournament_commands import TournamentCog
from database.db_session import db_session
from models.tournament import Tournament, TournamentMatch, TournamentRound
from services.league_roles import RoleSyncPlan, apply_role_sync, format_role_sync, missing_roles_text
from services.tournament_roles import teams_missing_role
from services.tournament_service import (
    create_tournament, find_participant_by_name, register_team, store_role_ids,
)
from test_tournament_cog_smoke import _ctx, _set_role_id, _started_swiss

GUILD_ID = 123
GONE = "999"     # the one role id the guild no longer resolves


def _guild_without_role_999(ctx):
    ctx.guild.roles = []
    ctx.guild.get_role = MagicMock(side_effect=lambda rid: None if rid == int(GONE) else MagicMock())
    return ctx.guild


async def _give_everyone_roles(tournament_id):
    async with db_session() as session:
        for i, name in enumerate(["Alpha", "Bravo", "Other0", "Other1"]):
            part = await find_participant_by_name(session, tournament_id, name)
            part.role_id = str(100 + i)


async def _count(model):
    async with db_session() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


def _sent(ctx):
    return ctx.followup.send.await_args.args[0]


def test_missing_roles_text_names_the_teams_and_the_remedy():
    assert missing_roles_text(["A", "B"]) == (
        "❌ These teams have no team role: **A**, **B**. "
        "Run `/tournament sync_roles apply:True`, then run this again.")


def test_missing_roles_text_caps_the_list():
    text = missing_roles_text([f"T{i}" for i in range(25)])
    assert "**T19**" in text and "**T20**" not in text
    assert "and 5 more" in text


@pytest.mark.asyncio
async def test_teams_missing_role_lists_unresolvable_teams_in_order(test_db):
    t_id = await _started_swiss([("Alpha", "456"), ("Bravo", "457")], extra_teams=0)
    await _set_role_id(t_id, "Alpha", "555")
    guild = MagicMock()
    guild.get_role = MagicMock(side_effect=lambda rid: None if rid == int(GONE) else MagicMock())
    assert await teams_missing_role(guild, t_id) == ["Bravo"]
    await _set_role_id(t_id, "Bravo", GONE)
    assert await teams_missing_role(guild, t_id) == ["Bravo"]
    await _set_role_id(t_id, "Bravo", "556")
    assert await teams_missing_role(guild, t_id) == []


@pytest.mark.asyncio
async def test_next_round_refuses_before_pairing_when_a_team_lacks_its_role(test_db):
    t_id = await _started_swiss([("Alpha", "456"), ("Bravo", "457")], extra_teams=2)
    await _give_everyone_roles(t_id)
    await _set_role_id(t_id, "Bravo", GONE)
    rounds_before = await _count(TournamentRound)
    cog = TournamentCog(MagicMock())
    cog._post_round_messages = AsyncMock()
    ctx = _ctx()
    _guild_without_role_999(ctx)

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True):
        await TournamentCog.next_round.callback(cog, ctx)

    assert "**Bravo**" in _sent(ctx)
    assert ctx.followup.send.await_args.kwargs["ephemeral"] is True
    assert await _count(TournamentRound) == rounds_before
    cog._post_round_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_next_round_still_pairs_when_every_team_has_its_role(test_db):
    t_id = await _started_swiss([("Alpha", "456"), ("Bravo", "457")], extra_teams=2)
    await _give_everyone_roles(t_id)
    rounds_before = await _count(TournamentRound)
    cog = TournamentCog(MagicMock())
    cog._post_round_messages = AsyncMock()
    cog._destination = MagicMock()
    cog._refresh_board = AsyncMock()
    cog._post_standings = AsyncMock()
    ctx = _ctx()
    _guild_without_role_999(ctx)

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True):
        await TournamentCog.next_round.callback(cog, ctx)

    assert await _count(TournamentRound) == rounds_before + 1


@pytest.mark.asyncio
async def test_playoff_refuses_when_a_team_lacks_its_role(test_db):
    t_id = await _started_swiss([("Alpha", "456"), ("Bravo", "457")], extra_teams=2)
    await _give_everyone_roles(t_id)
    await _set_role_id(t_id, "Alpha", None)
    cog = TournamentCog(MagicMock())
    cog._run_playoff = AsyncMock()
    ctx = _ctx()
    _guild_without_role_999(ctx)
    matches_before = await _count(TournamentMatch)

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True):
        await TournamentCog.playoff.callback(cog, ctx, top=4, play_in_a=None, play_in_b=None)

    assert "**Alpha**" in _sent(ctx)
    cog._run_playoff.assert_not_awaited()
    assert await _count(TournamentMatch) == matches_before


@pytest.mark.asyncio
async def test_open_rooms_refuses_when_a_team_lacks_its_role(test_db):
    t_id = await _started_swiss([("Alpha", "456"), ("Bravo", "457")], extra_teams=2)
    await _give_everyone_roles(t_id)
    await _set_role_id(t_id, "Bravo", GONE)
    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    _guild_without_role_999(ctx)

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("cogs.tournament_commands.open_room_for_posted_match", AsyncMock()) as opener:
        await TournamentCog.open_rooms.callback(cog, ctx, round_number=None)

    assert "**Bravo**" in _sent(ctx)
    opener.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_command_with_no_active_tournament_keeps_its_own_message(test_db):
    cog = TournamentCog(MagicMock())
    ctx = _ctx()
    _guild_without_role_999(ctx)
    with patch("cogs.tournament_commands.tournament_enabled", return_value=True):
        await TournamentCog.next_round.callback(cog, ctx)
    assert _sent(ctx) == "There is no active tournament."


async def _registering(names):
    async with db_session() as session:
        t = await create_tournament(session, str(GUILD_ID), "Spring", 3)
        for i, name in enumerate(names):
            part, _ = await register_team(session, t.id, name, str(300 + i))
            part.status = "paid"
        return t.id


async def _status(tournament_id):
    async with db_session() as session:
        return (await session.get(Tournament, tournament_id)).status


@pytest.mark.asyncio
async def test_start_refuses_a_team_whose_role_name_is_taken(test_db):
    t_id = await _registering(["Mod", "Bravo"])
    cog = TournamentCog(MagicMock())
    cog._post_schedule = AsyncMock()
    ctx = _ctx()
    ctx.guild.roles = [SimpleNamespace(id=1, name="Team Mod")]

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("helpers.permissions.get_config", return_value={}), \
         patch("services.league_roles.get_config", return_value={}):
        await TournamentCog.start.callback(cog, ctx)

    text = _sent(ctx)
    assert "**Mod**" in text and "Team Mod" in text
    assert await _status(t_id) == "registration"
    cog._post_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_posts_lines_without_rooms_when_creation_is_refused(test_db):
    t_id = await _registering(["Alpha", "Bravo"])
    cog = TournamentCog(MagicMock())
    cog._refresh_board = AsyncMock()
    cog._post_schedule = AsyncMock()
    cog._post_standings = AsyncMock()
    ctx = _ctx()
    ctx.guild.roles = []
    ctx.guild.create_role = AsyncMock(side_effect=RuntimeError("discord boom"))
    ctx.guild.get_role = MagicMock(return_value=None)

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("helpers.permissions.get_config", return_value={}), \
         patch("services.league_roles.get_config", return_value={}):
        await TournamentCog.start.callback(cog, ctx)

    assert await _status(t_id) == "active"
    assert cog._post_schedule.await_args.kwargs["create_rooms"] is False
    texts = [c.args[0] for c in ctx.followup.send.await_args_list]
    assert any("**Alpha**" in t and "`/tournament open_rooms 1`" in t for t in texts)


@pytest.mark.asyncio
async def test_sync_roles_apply_names_a_team_it_could_not_fix(test_db):
    t_id = await _registering(["Mod"])
    guild = MagicMock()
    guild.id = GUILD_ID
    guild.roles = [SimpleNamespace(id=1, name="Team Mod")]
    guild.get_role = MagicMock(return_value=None)
    guild.members = []
    with patch("helpers.permissions.get_config", return_value={}), \
         patch("services.league_roles.get_config", return_value={}):
        plan = await apply_role_sync(guild)
    assert plan.unfixed == ["Mod"]
    text = format_role_sync(plan, True)
    assert "**Teams still without a role** (1): **Mod**" in text
    assert '**Mod** — role "Team Mod" already exists' in text
    assert "refused" not in text


@pytest.mark.asyncio
async def test_sync_roles_apply_says_discord_refused_when_nothing_clashes(test_db):
    t_id = await _registering(["Mod"])
    guild = MagicMock()
    guild.id = GUILD_ID
    guild.roles = []
    guild.get_role = MagicMock(return_value=None)
    guild.create_role = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
    guild.members = []
    with patch("helpers.permissions.get_config", return_value={}), \
         patch("services.league_roles.get_config", return_value={}):
        plan = await apply_role_sync(guild)
    assert plan.unfixed == ["Mod"]
    text = format_role_sync(plan, True)
    assert "**Mod** — Discord refused to create it" in text
    assert "already exists" not in text


def test_format_does_not_mention_unfixed_on_a_dry_run_or_when_empty():
    assert "still without" not in format_role_sync(RoleSyncPlan(unfixed=["X"]), False)
    assert "still without" not in format_role_sync(RoleSyncPlan(), True)
