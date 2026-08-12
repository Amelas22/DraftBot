"""The registration board: renders roster + payment state, and edits in place."""
import pytest

from models.tournament import Tournament, TournamentParticipant
from services.tournament_formatter import create_registration_embed


def _tournament(name="Fee Cup", fee=1, status="registration"):
    return Tournament(id=1, guild_id="g1", name=name, total_rounds=0, format="manual",
                      status=status, entry_fee=fee, payout_structure="winner_take_all")


def _team(pid, name, captain, status):
    return TournamentParticipant(id=pid, tournament_id=1, team_id=pid, team_name=name,
                                 captain_user_id=captain, status=status)


def _text(embed):
    return embed.description + "".join(f.name + f.value for f in embed.fields)


def test_paid_board_marks_paid_and_pending_with_a_deficit():
    teams = [_team(1, "Alpha", "10", "paid"), _team(2, "Gamma", "20", "pending")]
    embed = create_registration_embed(_tournament(), teams, pot=1, deficits={2: 1})
    text = _text(embed)

    assert "Registration open" in embed.title
    assert "Teams (1/2 paid)" in text
    assert "✅ **Alpha**" in text and "⏳ **Gamma**" in text
    assert "needs 1 more tix" in text and "/wallet deposit 1" in text
    assert "Entry fee:** 1 tix/team" in text and "Prize pool:** 1 tix" in text


def test_free_board_is_a_plain_roster():
    teams = [_team(1, "Alpha", "10", "paid")]
    embed = create_registration_embed(_tournament(fee=0), teams)
    text = _text(embed)

    assert "Teams (1)" in text
    assert "Entry fee" not in text and "Prize pool" not in text
    assert "needs" not in text and "⏳" not in text


def test_all_paid_board_shows_no_deficit_lines():
    teams = [_team(1, "Alpha", "10", "paid"), _team(2, "Beta", "20", "paid")]
    text = _text(create_registration_embed(_tournament(), teams, pot=2, deficits={}))

    assert "Teams (2/2 paid)" in text
    assert "needs" not in text and "⏳" not in text


def test_closed_board_drops_deficits_and_says_closed():
    teams = [_team(1, "Alpha", "10", "paid"), _team(2, "Gamma", "20", "pending")]
    embed = create_registration_embed(_tournament(), teams, pot=1, deficits={2: 1}, closed=True)

    assert "Registration closed" in embed.title
    assert "needs" not in _text(embed)


def test_empty_board_invites_registration():
    assert "/tournament register" in _text(create_registration_embed(_tournament(), []))


from unittest.mock import AsyncMock, MagicMock

import discord

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from services.tournament_formatter import update_registration_board


async def _seed_tournament(posted=True):
    async with db_session() as session:
        t = Tournament(guild_id="g1", name="Fee Cup", total_rounds=0, format="manual",
                       status="registration", entry_fee=1,
                       board_channel_id="777" if posted else None,
                       board_message_id="888" if posted else None)
        session.add(t)
        await session.flush()
        return t.id


@pytest.mark.asyncio
async def test_updater_is_a_no_op_when_the_board_was_never_posted(test_db):  # noqa: F811
    t_id = await _seed_tournament(posted=False)
    bot = MagicMock()
    bot.get_channel = MagicMock()

    await update_registration_board(bot, t_id)

    bot.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_updater_edits_the_stored_message(test_db):  # noqa: F811
    t_id = await _seed_tournament()
    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    await update_registration_board(bot, t_id)

    message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_deleted_board_clears_its_ids_so_it_stops_retrying(test_db):  # noqa: F811
    t_id = await _seed_tournament()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    await update_registration_board(bot, t_id)

    async with db_session() as session:
        t = await session.get(Tournament, t_id)
        assert t.board_message_id is None and t.board_channel_id is None
