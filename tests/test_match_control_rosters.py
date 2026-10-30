"""A tournament match's two sides as the scheduler page lists them."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import update

from conftest import seed_tournament_match, test_db  # noqa: F401
from database.db_session import db_session
from helpers.matchup import Roster
from match_control_view import match_side_rosters
from models.tournament import Tournament, TournamentMatch, TournamentParticipant, TournamentTeamMember

GUILD = "222"


def _member(display_name, bot=False):
    return SimpleNamespace(display_name=display_name, bot=bot)


def _bot(members=None, fetched=None):
    """A bot whose guild cache holds ``members`` (id -> member)."""
    guild = MagicMock()
    guild.get_member = lambda uid: (members or {}).get(str(uid))
    client = MagicMock()
    client.get_guild = lambda gid: guild
    if fetched is None:
        client.fetch_user = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=404), "gone"))
    else:
        client.fetch_user = AsyncMock(return_value=fetched)
    return client


async def _seed():
    """(match id, side A participant, side B participant)."""
    async with db_session() as s:
        match = await seed_tournament_match(s)
        await s.execute(update(Tournament).values(guild_id=GUILD))
        a = await s.get(TournamentParticipant, match.team_a_participant_id)
        b = await s.get(TournamentParticipant, match.team_b_participant_id)
        return match.id, a.id, b.id


async def _add(participant_id, user_id, name):
    async with db_session() as s:
        s.add(TournamentTeamMember(participant_id=participant_id, user_id=user_id, display_name=name))


@pytest.mark.asyncio
async def test_captain_comes_first_then_the_roster_in_order(test_db):  # noqa: F811
    match_id, a, b = await _seed()
    async with db_session() as s:
        cap_a = (await s.get(TournamentParticipant, a)).captain_user_id
        label_a = (await s.get(TournamentParticipant, a)).team_name
    await _add(a, "50", "Fifty")
    await _add(a, "40", "Forty")
    bot = _bot({cap_a: _member("Cap"), "50": _member("Fifty Live"), "40": _member("Forty Live")})

    sides = await match_side_rosters(bot, GUILD, match_id)

    assert sides is not None
    assert sides[0] == Roster(label_a, ((cap_a, "Cap"), ("50", "Fifty Live"), ("40", "Forty Live")))


@pytest.mark.asyncio
async def test_a_bot_on_a_roster_is_dropped(test_db):  # noqa: F811
    match_id, a, _b = await _seed()
    await _add(a, "50", "Fifty")
    await _add(a, "666", "Robot")
    bot = _bot({"50": _member("Fifty"), "666": _member("Robot", bot=True)})

    sides = await match_side_rosters(bot, GUILD, match_id)

    assert sides is not None
    assert [u for u, _n in sides[0].people if u in ("50", "666")] == ["50"]


@pytest.mark.asyncio
async def test_someone_on_both_sides_is_listed_once_on_side_a(test_db):  # noqa: F811
    match_id, a, b = await _seed()
    await _add(a, "77", "Folero")
    await _add(b, "77", "Folero")

    sides = await match_side_rosters(_bot(), GUILD, match_id)

    assert sides is not None
    assert "77" in [u for u, _n in sides[0].people]
    assert "77" not in [u for u, _n in sides[1].people]


@pytest.mark.asyncio
async def test_a_member_who_left_shows_the_roster_snapshot_name(test_db):  # noqa: F811
    match_id, a, _b = await _seed()
    await _add(a, "50", "Fifty Then")

    sides = await match_side_rosters(_bot(), GUILD, match_id)

    assert sides is not None
    assert ("50", "Fifty Then") in sides[0].people


@pytest.mark.asyncio
async def test_a_captain_who_left_shows_their_global_name(test_db):  # noqa: F811
    match_id, a, _b = await _seed()
    async with db_session() as s:
        cap_a = (await s.get(TournamentParticipant, a)).captain_user_id

    bot = _bot(fetched=SimpleNamespace(global_name="Global Cap", name="capname"))
    sides = await match_side_rosters(bot, GUILD, match_id)

    assert sides is not None
    assert (cap_a, "Global Cap") in sides[0].people


@pytest.mark.asyncio
async def test_no_name_at_all_falls_back_to_the_id(test_db):  # noqa: F811
    match_id, a, _b = await _seed()
    async with db_session() as s:
        cap_a = (await s.get(TournamentParticipant, a)).captain_user_id

    sides = await match_side_rosters(_bot(), GUILD, match_id)

    assert sides is not None
    assert (cap_a, cap_a) in sides[0].people


@pytest.mark.asyncio
async def test_none_for_a_bye_an_unknown_side_another_guild_or_no_match(test_db):  # noqa: F811
    match_id, _a, _b = await _seed()
    assert await match_side_rosters(_bot(), "333", match_id) is None
    assert await match_side_rosters(_bot(), GUILD, 99999) is None
    async with db_session() as s:
        match = await s.get(TournamentMatch, match_id)
        match.is_bye = True
    assert await match_side_rosters(_bot(), GUILD, match_id) is None
    async with db_session() as s:
        match = await s.get(TournamentMatch, match_id)
        match.is_bye = False
        match.team_b_participant_id = None
    assert await match_side_rosters(_bot(), GUILD, match_id) is None
