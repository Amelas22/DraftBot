"""Tests for Slice 4: the auto-updating tournament standings message."""
import os
import random
import tempfile
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
from models.tournament import Tournament, TournamentParticipant
from services.tournament_formatter import (
    create_standings_embed,
    update_standings_message,
    update_standings_message_for_match,
)
from services.tournament_service import (
    create_tournament,
    register_team,
    set_result,
    start_tournament,
)


@pytest_asyncio.fixture
async def test_db():
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()
    os.unlink(temp_db.name)


def _fake_db_session(factory):
    @asynccontextmanager
    async def fake():
        async with factory() as inner:
            yield inner
            await inner.commit()
    return fake


def _participant(team_name, points, wins=0, losses=0, draws=0):
    return TournamentParticipant(
        tournament_id=1, team_id=1, team_name=team_name, captain_user_id="1",
        points=points, match_wins=wins, match_losses=losses, match_draws=draws,
    )


# ---- model columns ----------------------------------------------------------------

def test_tournament_has_standings_message_columns():
    t = Tournament(guild_id="1", name="Spring", total_rounds=3)
    assert t.standings_channel_id is None
    assert t.standings_message_id is None


# ---- create_standings_embed (pure) ------------------------------------------------

def test_standings_embed_lists_teams_in_given_order():
    tournament = Tournament(guild_id="1", name="Spring Cup", total_rounds=3)
    tournament.status = "active"
    tournament.current_round = 1
    participants = [
        _participant("Alpha", 3, wins=1),
        _participant("Bravo", 0, losses=1),
    ]
    embed = create_standings_embed(tournament, participants)
    assert "Spring Cup" in embed.title
    body = "\n".join(f.value for f in embed.fields)
    assert "Alpha" in body and "Bravo" in body
    assert body.index("Alpha") < body.index("Bravo")
    assert "3" in body  # points shown


def test_standings_embed_shows_omw_percentage_for_each_team():
    """OMW% is the first real tiebreak, so the board has to show it.

    It is passed in rather than recomputed here: the sort already built these
    numbers from the match graph, and a second derivation in the renderer is
    how the shown value drifts from the one that ordered the rows.
    """
    tournament = Tournament(guild_id="1", name="Spring Cup", total_rounds=3)
    tournament.status = "active"
    tournament.current_round = 2
    alpha = _participant("Alpha", 6, wins=2, losses=1)
    bravo = _participant("Bravo", 6, wins=2, losses=2)
    alpha.id, bravo.id = 11, 22

    embed = create_standings_embed(
        tournament, [alpha, bravo], omw={11: 0.61423, 22: 1 / 3})

    body = "\n".join(f.value for f in embed.fields)
    assert "61.4%" in body
    assert "33.3%" in body


def test_standings_row_leads_with_a_monospace_span_then_the_team_name():
    """Rank, points, record and OMW% sit in one inline code span; the name follows.

    Discord aligns nothing in proportional text, and a ``` block aligns but
    strips markdown. An inline span is monospace, so equal-length spans render
    equal widths and every name starts at the same x -- while the name stays
    outside the span, where bold and strikethrough still apply.
    """
    tournament = Tournament(guild_id="1", name="Spring Cup", total_rounds=3)
    tournament.status = "active"
    tournament.current_round = 2
    alpha = _participant("Alpha", 6, wins=2, losses=1)
    alpha.id = 11

    embed = create_standings_embed(tournament, [alpha], omw={11: 0.625})

    row = "\n".join(f.value for f in embed.fields).strip()
    span, _, name = row.partition("` ")
    assert span.startswith("`")
    assert "6" in span and "2-1" in span and "62.5%" in span
    assert name == "**Alpha**"


def test_standings_row_strikes_through_a_dropped_team_and_still_says_dropped():
    tournament = Tournament(guild_id="1", name="Spring Cup", total_rounds=3)
    tournament.status = "active"
    tournament.current_round = 2
    gone = _participant("Gone", 3, wins=1, losses=2)
    gone.id, gone.dropped_at = 22, "2026-09-10 00:00:00"

    embed = create_standings_embed(tournament, [gone], omw={22: 0.5})

    body = "\n".join(f.value for f in embed.fields)
    assert "~~Gone~~" in body
    assert "*(dropped)*" in body


def test_standings_spans_stay_the_same_width_when_a_team_has_a_draw():
    """A drawn record renders W-L-D, two characters wider than W-L.

    The column is sized from the widest record actually present, so one drawn
    match cannot knock every row below it out of alignment.
    """
    tournament = Tournament(guild_id="1", name="Spring Cup", total_rounds=3)
    tournament.status = "active"
    tournament.current_round = 3
    plain = _participant("Plain", 6, wins=2, losses=1)
    drawn = _participant("Drawn", 7, wins=2, losses=0, draws=1)
    plain.id, drawn.id = 1, 2

    embed = create_standings_embed(tournament, [plain, drawn],
                                   omw={1: 0.5, 2: 0.5})

    spans = [line.split("`")[1]
             for line in "\n".join(f.value for f in embed.fields).splitlines()]
    assert len(spans) == 2
    assert len(spans[0]) == len(spans[1]), spans


def test_standings_embed_labels_playoff_rounds_instead_of_counting_past_the_end():
    """Playoff rounds are numbered past total_rounds (round N+1 is the first
    bracket round), so the swiss "N of M" form renders "Round: 4/3" once the
    cut is made. The stage is passed in, not inferred from the round number."""
    tournament = Tournament(guild_id="1", name="Spring Cup", total_rounds=3)
    tournament.status = "active"
    tournament.current_round = 5           # second bracket round
    embed = create_standings_embed(tournament, [], "playoff")
    assert "5/3" not in embed.description
    assert "Playoff round 2" in embed.description


def test_standings_embed_still_counts_swiss_rounds_normally():
    tournament = Tournament(guild_id="1", name="Spring Cup", total_rounds=3)
    tournament.status = "active"
    tournament.current_round = 2
    embed = create_standings_embed(tournament, [])
    assert "**Round:** 2/3" in embed.description


def test_standings_embed_handles_no_participants():
    tournament = Tournament(guild_id="1", name="Spring Cup", total_rounds=3)
    tournament.status = "registration"
    tournament.current_round = 0
    embed = create_standings_embed(tournament, [])
    # Should not raise and should produce some placeholder content
    assert embed.fields


# ---- update_standings_message -----------------------------------------------------

@pytest.mark.asyncio
async def test_update_standings_message_edits_stored_message(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        await register_team(session, tournament.id, "Alpha", "1")
        await register_team(session, tournament.id, "Bravo", "2")
        await session.commit()
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await set_result(session, matches[0].id, 2, 0)
        tournament.standings_channel_id = "555"
        tournament.standings_message_id = "777"
        await session.commit()
        tid = tournament.id

    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_channel.return_value = channel

    with patch("services.tournament_formatter.db_session", _fake_db_session(test_db)):
        await update_standings_message(bot, tid)

    bot.get_channel.assert_called_once_with(555)
    channel.fetch_message.assert_awaited_once_with(777)
    message.edit.assert_awaited_once()
    assert "embed" in message.edit.call_args.kwargs


@pytest.mark.asyncio
async def test_update_standings_message_shows_the_tiebreak_it_sorted_by(test_db):
    """The posted board carries OMW%, not just the order OMW% produced.

    The renderer cannot derive it -- that needs the match graph -- so the live
    path has to hand it over. Without this the bot silently posts the new
    layout with the tiebreak column missing.
    """
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        await register_team(session, tournament.id, "Alpha", "1")
        await register_team(session, tournament.id, "Bravo", "2")
        await session.commit()
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await set_result(session, matches[0].id, 2, 0)
        tournament.standings_channel_id = "555"
        tournament.standings_message_id = "777"
        await session.commit()
        tid = tournament.id

    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_channel.return_value = channel

    with patch("services.tournament_formatter.db_session", _fake_db_session(test_db)):
        await update_standings_message(bot, tid)

    embed = message.edit.call_args.kwargs["embed"]
    body = "\n".join(f.value for f in embed.fields)
    assert "%" in body, body


@pytest.mark.asyncio
async def test_update_standings_message_noop_when_not_posted(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        tid = tournament.id

    bot = MagicMock()
    with patch("services.tournament_formatter.db_session", _fake_db_session(test_db)):
        await update_standings_message(bot, tid)
    bot.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_update_standings_message_survives_missing_message(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        tournament.standings_channel_id = "555"
        tournament.standings_message_id = "777"
        await session.commit()
        tid = tournament.id

    channel = MagicMock()
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    bot = MagicMock()
    bot.get_channel.return_value = channel

    with patch("services.tournament_formatter.db_session", _fake_db_session(test_db)):
        await update_standings_message(bot, tid)  # must not raise


# ---- update_standings_message_for_match -------------------------------------------

@pytest.mark.asyncio
async def test_update_for_match_resolves_tournament_and_edits(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        await register_team(session, tournament.id, "Alpha", "1")
        await register_team(session, tournament.id, "Bravo", "2")
        await session.commit()
        matches = await start_tournament(session, tournament.id, random.Random(7))
        tournament.standings_channel_id = "555"
        tournament.standings_message_id = "777"
        await session.commit()
        match_id = matches[0].id

    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_channel.return_value = channel

    with patch("services.tournament_formatter.db_session", _fake_db_session(test_db)):
        await update_standings_message_for_match(bot, match_id)

    message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_standings_message_names_the_bracket_round(test_db):
    """The pinned window is the one standings display everyone reads. Its round
    line now takes the stage rather than inferring it, so this covers the wiring:
    a caller that forgot to pass it would silently print "Round: 4/3" again."""
    from models.tournament import TournamentRound

    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3, cut_to=2)
        await session.commit()
        session.add(TournamentRound(tournament_id=tournament.id, round_number=4,
                                    stage="playoff"))
        tournament.current_round = 4
        tournament.standings_channel_id = "555"
        tournament.standings_message_id = "777"
        await session.commit()
        tid = tournament.id

    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_channel.return_value = channel

    with patch("services.tournament_formatter.db_session", _fake_db_session(test_db)):
        await update_standings_message(bot, tid)

    description = message.edit.call_args.kwargs["embed"].description
    assert "Playoff round 1" in description and "4/3" not in description


def test_standings_embed_splits_across_fields_past_discords_cap():
    """A field value over 1024 chars makes Discord reject the WHOLE embed, so a big
    field has to span several. Found on a 40-team prod copy: /tournament start seeded
    the tournament and created every room, then failed with
    "50035 ... embeds.0.fields.0.value: Must be 1024 or fewer in length" — leaving an
    active tournament with no standings, no board and no pairings message.
    """
    tournament = Tournament(guild_id="1", name="Lotus League 2026", total_rounds=4)
    tournament.status = "active"
    tournament.current_round = 1
    participants = [_participant(f"Team Number {i:02d}", points=3 * (40 - i), wins=40 - i)
                    for i in range(40)]

    embed = create_standings_embed(tournament, participants)

    assert len(embed.fields) > 1, "40 teams should not fit in one field"
    for field in embed.fields:
        assert len(field.value) <= 1024, f"'{field.name}' is {len(field.value)} chars"
    body = "\n".join(f.value for f in embed.fields)
    for i in range(40):
        assert f"Team Number {i:02d}" in body, f"team {i} was dropped by the split"
    assert embed.fields[0].name == "Standings"
    assert embed.fields[1].name == "Standings (cont.)"


def test_a_dropped_team_is_marked_but_keeps_its_place():
    """The record still counts, so it still ranks -- but the organizer has to be
    able to see who is still being paired."""
    from datetime import datetime

    tournament = Tournament(guild_id="1", name="Spring Cup", total_rounds=3)
    tournament.status = "active"
    tournament.current_round = 2
    gone = _participant("Bravo", 3, wins=1)
    gone.dropped_at = datetime(2026, 9, 6, 12, 0)
    participants = [_participant("Alpha", 6, wins=2), gone]

    body = "\n".join(f.value for f in create_standings_embed(tournament, participants).fields)

    assert "Bravo" in body, "a dropped team keeps its place in the standings"
    bravo_line = next(line for line in body.splitlines() if "Bravo" in line)
    alpha_line = next(line for line in body.splitlines() if "Alpha" in line)
    assert "dropped" in bravo_line.lower()
    assert "dropped" not in alpha_line.lower()


# ---- records omit the draw count --------------------------------------------------

def test_standings_embed_shows_a_win_loss_record():
    # Every team match has to produce a winner, so the draw count is zero on
    # every row and only makes the line harder to read.
    tournament = Tournament(guild_id="1", name="Spring Cup", total_rounds=3)
    tournament.status = "active"
    tournament.current_round = 2

    embed = create_standings_embed(
        tournament, [_participant("Alpha", 6, wins=2, losses=0)])

    body = "\n".join(f.value for f in embed.fields)
    assert "2-0" in body
    assert "2-0-0" not in body


def test_standings_embed_keeps_a_draw_that_was_actually_recorded():
    # The schema permits a draw even though the rules do not; a record that
    # had one must not silently lose it.
    tournament = Tournament(guild_id="1", name="Spring Cup", total_rounds=3)
    tournament.status = "active"
    tournament.current_round = 2

    embed = create_standings_embed(
        tournament, [_participant("Alpha", 4, wins=1, losses=0, draws=1)])

    assert "1-0-1" in "\n".join(f.value for f in embed.fields)
