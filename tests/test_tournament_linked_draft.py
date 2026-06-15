"""Tests for Slice 3: linked premade-draft auto-recording of tournament results."""
import os
import random
import tempfile
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
from models.session_details import SessionDetails
from models.tournament import TournamentParticipant
from services.tournament_service import (
    create_tournament,
    record_linked_result,
    register_team,
    start_tournament,
)

CUBES = [{"label": "AlphaFrog", "value": "AlphaFrog"}]


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


def make_interaction():
    interaction = MagicMock()
    interaction.user.id = 42
    interaction.guild_id = 123
    return interaction


# ---- session details / draft session threading -----------------------------------

def test_session_details_defaults_to_no_tournament_match():
    details = SessionDetails(make_interaction())
    assert details.tournament_match_id is None


def test_setup_draft_session_threads_tournament_match_id(test_db):
    from sessions.premade_session import PremadeSession

    details = SessionDetails(make_interaction())
    details.cube_choice = "AlphaFrog"
    details.team_a_name = "Alpha"
    details.team_b_name = "Bravo"
    details.tournament_match_id = 77

    draft = PremadeSession(details).setup_draft_session(MagicMock())
    assert draft.tournament_match_id == 77
    assert draft.session_type == "premade"


def test_setup_draft_session_without_tournament_stays_none(test_db):
    from sessions.premade_session import PremadeSession

    details = SessionDetails(make_interaction())
    details.cube_choice = "AlphaFrog"
    draft = PremadeSession(details).setup_draft_session(MagicMock())
    assert draft.tournament_match_id is None


# ---- cube selection view carries overrides ----------------------------------------

@pytest.mark.asyncio
async def test_cube_view_applies_session_details_overrides():
    with patch("cube_views.pack_options.get_cube_options", return_value=CUBES):
        from modals import CubeDraftSelectionView
        view = CubeDraftSelectionView(
            session_type="premade",
            guild_id=1,
            session_details_overrides={
                "tournament_match_id": 77,
                "team_a_name": "Alpha",
                "team_b_name": "Bravo",
            },
        )
    view.cube_choice = "AlphaFrog"
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    with patch("modals.handle_draft_session", new_callable=AsyncMock) as handler, \
         patch("modals.SessionDetails") as SD:
        details = MagicMock()
        SD.return_value = details
        await view.submit_callback(interaction)
    handler.assert_awaited_once()
    assert details.tournament_match_id == 77
    assert details.team_a_name == "Alpha"
    assert details.team_b_name == "Bravo"


# ---- record_linked_result -----------------------------------------------------------

@pytest.mark.asyncio
async def test_record_linked_result_records_match_and_stats(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        await register_team(session, tournament.id, "Alpha", "1")
        await register_team(session, tournament.id, "Bravo", "2")
        await session.commit()
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        match_id = matches[0].id
        part_a_id = matches[0].team_a_participant_id

    @asynccontextmanager
    async def fake_db_session():
        async with test_db() as inner:
            yield inner
            await inner.commit()

    with patch("services.tournament_service.db_session", fake_db_session):
        match = await record_linked_result(match_id, 2, 1)

    assert (match.team_a_wins, match.team_b_wins) == (2, 1)
    async with test_db() as session:
        winner = await session.get(TournamentParticipant, part_a_id)
        assert winner.match_wins == 1 and winner.points == 3


@pytest.mark.asyncio
async def test_record_linked_result_is_correction_safe(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        await register_team(session, tournament.id, "Alpha", "1")
        await register_team(session, tournament.id, "Bravo", "2")
        await session.commit()
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        match_id = matches[0].id
        part_a_id = matches[0].team_a_participant_id

    @asynccontextmanager
    async def fake_db_session():
        async with test_db() as inner:
            yield inner
            await inner.commit()

    with patch("services.tournament_service.db_session", fake_db_session):
        await record_linked_result(match_id, 2, 0)  # e.g. admin forfeit ruling
        await record_linked_result(match_id, 1, 2)  # teams played anyway

    async with test_db() as session:
        part_a = await session.get(TournamentParticipant, part_a_id)
        assert (part_a.match_wins, part_a.match_losses, part_a.points) == (0, 1, 0)
        assert (part_a.game_wins, part_a.game_losses) == (1, 2)


def test_round_model_stores_pairings_message_location(test_db):
    from models.tournament import TournamentRound

    round_ = TournamentRound(tournament_id=1, round_number=1)
    assert round_.pairings_message_id is None
    assert round_.pairings_channel_id is None


# ---- already-reported matches are not playable ------------------------------------

async def _started_round_robin(session, count=4):
    t = await create_tournament(session, "g1", "RR", 0, format="round_robin")
    await session.commit()
    for i in range(count):
        await register_team(session, t.id, f"T{i}", str(i))
    await session.commit()
    matches = await start_tournament(session, t.id, random.Random(7))
    await session.commit()
    return t, matches


@pytest.mark.asyncio
async def test_re_register_skips_reported_matches(test_db):
    from cogs.tournament_commands import re_register_tournament_views
    from models.tournament import TournamentMatch
    from services.tournament_service import set_result

    async with test_db() as session:
        _t, matches = await _started_round_robin(session, count=4)  # 6 matches, no byes
        for i, m in enumerate(matches):
            mm = await session.get(TournamentMatch, m.id)
            mm.pairings_message_id = str(1000 + i)
        await session.commit()
        await set_result(session, matches[0].id, 2, 0)  # report one
        await session.commit()
        total = len(matches)

    @asynccontextmanager
    async def fake_db_session():
        async with test_db() as inner:
            yield inner
            await inner.commit()

    bot = MagicMock()
    bot.add_view = MagicMock()
    with patch("cogs.tournament_commands.db_session", fake_db_session):
        await re_register_tournament_views(bot)

    assert bot.add_view.call_count == total - 1  # reported match not re-registered


@pytest.mark.asyncio
async def test_play_match_view_has_one_button():
    from cogs.tournament_commands import PlayMatchView

    view = PlayMatchView(5, "Alpha vs Bravo")
    assert view.timeout is None
    assert len(view.children) == 1
    assert view.children[0].custom_id == "tournament_play:5"


@pytest.mark.asyncio
async def test_launch_creates_thread_and_stores_id(test_db):
    from cogs.tournament_commands import launch_tournament_match

    async with test_db() as session:
        _t, matches = await _started_round_robin(session, count=2)
        match_id = matches[0].id

    @asynccontextmanager
    async def fake_db_session():
        async with test_db() as inner:
            yield inner
            await inner.commit()

    thread = MagicMock()
    thread.id = 999
    thread.mention = "<#999>"
    thread.send = AsyncMock()
    interaction = MagicMock()
    interaction.message.create_thread = AsyncMock(return_value=thread)
    interaction.response.send_message = AsyncMock()

    with patch("cogs.tournament_commands.db_session", fake_db_session):
        await launch_tournament_match(interaction, match_id)

    interaction.message.create_thread.assert_awaited_once()
    thread.send.assert_awaited_once()  # cube picker posted in the thread
    # thread id persisted on the match
    async with test_db() as session:
        from models.tournament import TournamentMatch
        m = await session.get(TournamentMatch, match_id)
        assert m.thread_id == "999"


@pytest.mark.asyncio
async def test_launch_reuses_existing_thread(test_db):
    from cogs.tournament_commands import launch_tournament_match
    from models.tournament import TournamentMatch

    async with test_db() as session:
        _t, matches = await _started_round_robin(session, count=2)
        match_id = matches[0].id
        m = await session.get(TournamentMatch, match_id)
        m.thread_id = "12345"  # Ben already made this thread
        await session.commit()

    @asynccontextmanager
    async def fake_db_session():
        async with test_db() as inner:
            yield inner
            await inner.commit()

    thread = MagicMock()
    thread.mention = "<#12345>"
    thread.send = AsyncMock()
    interaction = MagicMock()
    interaction.guild.get_channel.return_value = thread
    interaction.message.create_thread = AsyncMock()
    interaction.response.send_message = AsyncMock()

    with patch("cogs.tournament_commands.db_session", fake_db_session):
        await launch_tournament_match(interaction, match_id)

    interaction.message.create_thread.assert_not_called()  # reused, not recreated
    interaction.guild.get_channel.assert_called_once_with(12345)
    thread.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_launch_refuses_already_reported_match(test_db):
    from cogs.tournament_commands import launch_tournament_match
    from services.tournament_service import set_result

    async with test_db() as session:
        _t, matches = await _started_round_robin(session, count=2)
        match_id = matches[0].id
        await set_result(session, match_id, 2, 0)
        await session.commit()

    @asynccontextmanager
    async def fake_db_session():
        async with test_db() as inner:
            yield inner
            await inner.commit()

    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    with patch("cogs.tournament_commands.db_session", fake_db_session):
        await launch_tournament_match(interaction, match_id)

    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.call_args.args[0]
    assert "already" in msg.lower() and "result" in msg.lower()
    # must NOT have launched a draft (no view passed)
    assert "view" not in interaction.response.send_message.call_args.kwargs
