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


def _fake_db_session(test_db):
    """An async-context-manager `db_session` replacement over a throwaway db."""
    @asynccontextmanager
    async def fake_db_session():
        async with test_db() as inner:
            yield inner
            await inner.commit()
    return fake_db_session


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
            mm.control_message_id = str(2000 + i)  # every match already has a room
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
async def test_re_register_registers_control_views_not_play_buttons(test_db):
    from cogs.tournament_commands import re_register_tournament_views
    from models.tournament import TournamentMatch

    async with test_db() as session:
        _t, matches = await _started_round_robin(session, count=4)
        for i, m in enumerate(matches):
            mm = await session.get(TournamentMatch, m.id)
            mm.pairings_message_id = str(1000 + i)
            mm.control_message_id = str(2000 + i)
        await session.commit()
        expected = len(matches)

    bot = MagicMock()
    bot.add_view = MagicMock()
    with patch("cogs.tournament_commands.db_session", _fake_db_session(test_db)):
        await re_register_tournament_views(bot)

    assert bot.add_view.call_count == expected
    # Every registered view is the control message's, keyed to it.
    for call in bot.add_view.call_args_list:
        assert call.kwargs["message_id"] >= 2000


@pytest.mark.asyncio
async def test_re_register_skips_matches_with_no_control_message(test_db):
    from cogs.tournament_commands import re_register_tournament_views
    from models.tournament import TournamentMatch

    async with test_db() as session:
        # pairings_message_id IS set on every match, so it's only the missing
        # control_message_id that must exclude them -- otherwise this test
        # would pass even with the control_message_id filter deleted, because
        # the pre-existing pairings_message_id filter would exclude them too.
        _t, matches = await _started_round_robin(session, count=4)
        for i, m in enumerate(matches):
            mm = await session.get(TournamentMatch, m.id)
            mm.pairings_message_id = str(1000 + i)
        await session.commit()

    bot = MagicMock()
    bot.add_view = MagicMock()
    with patch("cogs.tournament_commands.db_session", _fake_db_session(test_db)):
        await re_register_tournament_views(bot)

    bot.add_view.assert_not_called()


# ---- _post_round_messages gives only the playable match a room --------------------

@pytest.mark.asyncio
async def test_post_round_messages_gives_only_the_playable_match_a_room(test_db):
    """One bye, one already-reported match, one playable match: only the
    playable one should get create_match_room called and its line edited."""
    from cogs.tournament_commands import TournamentCog
    from models.tournament import TournamentMatch, TournamentRound
    from services.tournament_service import create_tournament, register_team

    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Cup", 3)
        await session.commit()
        alpha, _ = await register_team(session, tournament.id, "Alpha", "1")
        bravo, _ = await register_team(session, tournament.id, "Bravo", "2")
        charlie, _ = await register_team(session, tournament.id, "Charlie", "3")
        delta, _ = await register_team(session, tournament.id, "Delta", "4")
        echo, _ = await register_team(session, tournament.id, "Echo", "5")
        await session.commit()

        round_ = TournamentRound(tournament_id=tournament.id, round_number=1)
        session.add(round_)
        await session.flush()

        bye = TournamentMatch(round_id=round_.id, team_a_participant_id=alpha.id,
                               team_b_participant_id=None, is_bye=True)
        reported = TournamentMatch(round_id=round_.id, team_a_participant_id=bravo.id,
                                    team_b_participant_id=charlie.id,
                                    team_a_wins=2, team_b_wins=0)
        playable = TournamentMatch(round_id=round_.id, team_a_participant_id=delta.id,
                                    team_b_participant_id=echo.id)
        session.add_all([bye, reported, playable])
        await session.commit()
        round_id = round_.id
        playable_id, reported_id, bye_id = playable.id, reported.id, bye.id
        bye_text = "• **Alpha** — BYE (auto win)"
        reported_text = "• **Bravo** 2–0 **Charlie**"

    cog = TournamentCog.__new__(TournamentCog)  # no bot needed; not touched

    def make_message(*_a, **_k):
        msg = MagicMock()
        msg.id = 500 + make_message.count
        msg.channel.id = 555
        msg.edit = AsyncMock()
        make_message.count += 1
        return msg
    make_message.count = 0

    channel = MagicMock()
    channel.send = AsyncMock(side_effect=make_message)

    thread = MagicMock()
    thread.id = 9999

    with patch("cogs.tournament_commands.db_session", _fake_db_session(test_db)), \
         patch("cogs.tournament_commands.create_match_room",
               AsyncMock(return_value=thread)) as create_room:
        await cog._post_round_messages(channel, round_id, 1)

    # Header + one line per match, exactly three lines beyond the header.
    assert channel.send.call_count == 4
    texts = [c.args[0] for c in channel.send.call_args_list]
    assert bye_text in texts
    assert reported_text in texts
    assert any("Delta" in t and "Echo" in t for t in texts)

    # Only the playable match's room gets created.
    create_room.assert_awaited_once()
    room_message, room_match_id = create_room.call_args.args
    assert room_match_id == playable_id

    # Only that match's line is edited, to carry the room link.
    room_message.edit.assert_awaited_once()
    assert "<#9999>" in room_message.edit.call_args.kwargs["content"]

    async with test_db() as session:
        stored_playable = await session.get(TournamentMatch, playable_id)
        assert stored_playable.pairings_message_id == str(room_message.id)
        assert stored_playable.pairings_channel_id == str(room_message.channel.id)
        # The bye and the reported match never got pairing ids persisted --
        # they're posted as plain text and never enter the room-creating branch.
        stored_bye = await session.get(TournamentMatch, bye_id)
        assert stored_bye.pairings_message_id is None
        stored_reported = await session.get(TournamentMatch, reported_id)
        assert stored_reported.pairings_message_id is None


# ---- create_rooms=False: post everything, room nothing ----------------------------

@pytest.mark.asyncio
async def test_post_round_messages_without_create_rooms_skips_rooms(test_db):
    """create_rooms=False must still post the header and every match's line and
    persist its pairing ids (so /tournament open_rooms has something to act on
    later), but must never call create_match_room -- and the header must name
    the remedy."""
    from cogs.tournament_commands import TournamentCog
    from models.tournament import TournamentMatch, TournamentRound
    from services.tournament_service import create_tournament, register_team

    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Cup", 0, format="round_robin")
        await session.commit()
        alpha, _ = await register_team(session, tournament.id, "Alpha", "1")
        bravo, _ = await register_team(session, tournament.id, "Bravo", "2")
        await session.commit()

        round_ = TournamentRound(tournament_id=tournament.id, round_number=2)
        session.add(round_)
        await session.flush()
        playable = TournamentMatch(round_id=round_.id, team_a_participant_id=alpha.id,
                                    team_b_participant_id=bravo.id)
        session.add(playable)
        await session.commit()
        round_id, round_number, playable_id = round_.id, round_.round_number, playable.id

    cog = TournamentCog.__new__(TournamentCog)

    def make_message(*_a, **_k):
        msg = MagicMock()
        msg.id = 700
        msg.channel.id = 555
        msg.edit = AsyncMock()
        return msg

    channel = MagicMock()
    channel.send = AsyncMock(side_effect=make_message)

    with patch("cogs.tournament_commands.db_session", _fake_db_session(test_db)), \
         patch("cogs.tournament_commands.create_match_room", AsyncMock()) as create_room:
        await cog._post_round_messages(channel, round_id, round_number, create_rooms=False)

    header = channel.send.call_args_list[0].args[0]
    assert f"Week {round_number} pairings" in header
    assert "/tournament open_rooms" in header
    assert channel.send.call_count == 2  # header + the one match's line
    create_room.assert_not_awaited()

    async with test_db() as session:
        stored = await session.get(TournamentMatch, playable_id)
        assert stored.pairings_message_id == "700"
        assert stored.pairings_channel_id == "555"
        assert stored.thread_id is None


# ---- _post_schedule only rooms the first round -------------------------------------

@pytest.mark.asyncio
async def test_post_schedule_only_rooms_the_first_round(test_db):
    """An 8-team round robin (or any all-open format) reveals every round at
    once; only the first round may pay the per-match room-creation cost inline
    -- the rest post room-less and get caught up by /tournament open_rooms."""
    from cogs.tournament_commands import TournamentCog

    async with test_db() as session:
        tournament, _matches = await _started_round_robin(session, count=4)  # 3 rounds
        tournament_id = tournament.id

    cog = TournamentCog.__new__(TournamentCog)
    channel = MagicMock()
    calls = []

    async def fake_post(_channel, _round_id, round_number, create_rooms=True):
        calls.append((round_number, create_rooms))

    cog._post_round_messages = fake_post
    with patch("cogs.tournament_commands.db_session", _fake_db_session(test_db)):
        await cog._post_schedule(channel, tournament_id)

    assert calls == [(1, True), (2, False), (3, False)]


@pytest.mark.asyncio
async def test_post_schedule_swiss_still_rooms_its_one_round(test_db):
    """Swiss must post and room exactly as it did before this change. /start
    only ever creates round 1 for Swiss (later rounds come through
    /tournament next_round, which is untouched), so _post_schedule must see
    exactly one round and create its room inline."""
    from cogs.tournament_commands import TournamentCog
    from services.tournament_service import create_tournament, register_team, start_tournament

    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Cup", 3)  # swiss (default)
        await session.commit()
        await register_team(session, tournament.id, "Alpha", "1")
        await register_team(session, tournament.id, "Bravo", "2")
        await session.commit()
        await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        tournament_id = tournament.id

    cog = TournamentCog.__new__(TournamentCog)
    channel = MagicMock()
    calls = []

    async def fake_post(_channel, _round_id, round_number, create_rooms=True):
        calls.append((round_number, create_rooms))

    cog._post_round_messages = fake_post
    with patch("cogs.tournament_commands.db_session", _fake_db_session(test_db)):
        await cog._post_schedule(channel, tournament_id)

    assert calls == [(1, True)]


# ---- the posting loop survives one match's line failing to post -------------------

@pytest.mark.asyncio
async def test_post_round_messages_continues_past_a_failing_match(test_db):
    """A discord.HTTPException posting one match's line must be logged and
    skipped, not abort the round -- every match after it must still post."""
    import discord

    from cogs.tournament_commands import TournamentCog
    from models.tournament import TournamentMatch, TournamentRound
    from services.tournament_service import create_tournament, register_team

    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Cup", 3)
        await session.commit()
        alpha, _ = await register_team(session, tournament.id, "Alpha", "1")
        bravo, _ = await register_team(session, tournament.id, "Bravo", "2")
        charlie, _ = await register_team(session, tournament.id, "Charlie", "3")
        delta, _ = await register_team(session, tournament.id, "Delta", "4")
        await session.commit()

        round_ = TournamentRound(tournament_id=tournament.id, round_number=1)
        session.add(round_)
        await session.flush()
        m1 = TournamentMatch(round_id=round_.id, team_a_participant_id=alpha.id,
                              team_b_participant_id=bravo.id)
        m2 = TournamentMatch(round_id=round_.id, team_a_participant_id=charlie.id,
                              team_b_participant_id=delta.id)
        session.add_all([m1, m2])
        await session.commit()
        round_id, m1_id, m2_id = round_.id, m1.id, m2.id

    def make_message(*_a, **_k):
        msg = MagicMock()
        msg.id = 900
        msg.channel.id = 555
        msg.edit = AsyncMock()
        return msg

    channel = MagicMock()
    # header succeeds, match 1's line raises, match 2's line succeeds.
    channel.send = AsyncMock(side_effect=[
        MagicMock(),
        discord.HTTPException(MagicMock(), "boom"),
        make_message(),
    ])

    with patch("cogs.tournament_commands.db_session", _fake_db_session(test_db)), \
         patch("cogs.tournament_commands.create_match_room", AsyncMock(return_value=None)):
        cog = TournamentCog.__new__(TournamentCog)
        await cog._post_round_messages(channel, round_id, 1)

    assert channel.send.call_count == 3  # header + failed attempt + surviving line
    async with test_db() as session:
        m1_stored = await session.get(TournamentMatch, m1_id)
        m2_stored = await session.get(TournamentMatch, m2_id)
        # The failed match never got its pairing ids persisted...
        assert m1_stored.pairings_message_id is None
        # ...but the match after it in the loop still did.
        assert m2_stored.pairings_message_id == "900"


# ---- /tournament open_rooms ---------------------------------------------------------

@pytest.mark.asyncio
async def test_open_rooms_opens_only_roomless_matches_and_is_idempotent(test_db):
    """Round 1 already has rooms (posted normally); rounds 2 and 3 were posted
    room-less. With no round given, open_rooms must open exactly round 2's
    room-less matches and leave round 3 untouched. Running it again for round 2
    must find nothing to do (idempotent); a further run with no round given
    then reaches round 3."""
    from sqlalchemy import select

    from cogs.tournament_commands import TournamentCog
    from models.tournament import TournamentMatch, TournamentRound

    async with test_db() as session:
        tournament, matches = await _started_round_robin(session, count=4)  # 3 rounds, 2/round
        tournament_id = tournament.id
        rounds = (await session.execute(
            select(TournamentRound).where(TournamentRound.tournament_id == tournament_id)
            .order_by(TournamentRound.round_number)
        )).scalars().all()
        round_number_of = {r.id: r.round_number for r in rounds}
        for m in matches:
            mm = await session.get(TournamentMatch, m.id)
            mm.pairings_channel_id = "555"
            mm.pairings_message_id = str(1000 + m.id)
            if round_number_of[m.round_id] == 1:
                mm.thread_id = str(9000 + m.id)  # round 1 was posted normally, already roomed
        await session.commit()
        round2_ids = {m.id for m in matches if round_number_of[m.round_id] == 2}
        round3_ids = {m.id for m in matches if round_number_of[m.round_id] == 3}

    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)

    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    cog = TournamentCog(bot)

    ctx = MagicMock()
    ctx.guild.id = "g1"
    ctx.author.id = 999
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()

    opened_ids = []

    async def fake_create_room(_message, match_id):
        """Stands in for create_match_room, including its DB-visible effect
        (setting thread_id) -- open_rooms' idempotency depends on that being
        true, exactly like it would against the real implementation."""
        opened_ids.append(match_id)
        thread = MagicMock()
        thread.id = 9000 + match_id
        async with test_db() as s:
            mm = await s.get(TournamentMatch, match_id)
            mm.thread_id = str(thread.id)
            await s.commit()
        return thread

    with patch("cogs.tournament_commands.tournament_enabled", return_value=True), \
         patch("cogs.tournament_commands.db_session", _fake_db_session(test_db)), \
         patch("cogs.tournament_commands.create_match_room", fake_create_room):
        await TournamentCog.open_rooms.callback(cog, ctx, round_number=None)
        first_reply = ctx.followup.send.call_args.args[0]
        assert set(opened_ids) == round2_ids
        assert "Opened 2 room(s) for Week 2" in first_reply

        opened_ids.clear()
        await TournamentCog.open_rooms.callback(cog, ctx, round_number=2)
        second_reply = ctx.followup.send.call_args.args[0]
        assert opened_ids == []
        assert "Nothing to do" in second_reply

        await TournamentCog.open_rooms.callback(cog, ctx, round_number=None)
        third_reply = ctx.followup.send.call_args.args[0]
        assert set(opened_ids) == round3_ids
        assert "Opened 2 room(s) for Week 3" in third_reply

    async with test_db() as session:
        for match_id in round2_ids | round3_ids:
            stored = await session.get(TournamentMatch, match_id)
            assert stored.thread_id is not None
            # Each match's line was edited to carry its room link.
        assert message.edit.await_count == 4  # 2 matches x 2 opening runs


# ---- a bracket bye is not a swiss bye ---------------------------------------------

@pytest.mark.asyncio
async def test_a_bracket_bye_is_not_posted_as_an_auto_win(test_db):
    """A swiss bye is a RESULT: _award_bye grants points and a match win, so
    "BYE (auto win)" is accurate there. A bracket bye is the absence of a
    match -- swiss records are frozen and nothing is scored -- so posting it
    as an auto win tells organizers the opposite of what the code does."""
    from cogs.tournament_commands import TournamentCog
    from models.tournament import TournamentMatch, TournamentRound

    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Cup", 3, cut_to=4)
        await session.commit()
        alpha, _ = await register_team(session, tournament.id, "Alpha", "1")
        await session.commit()
        round_ = TournamentRound(tournament_id=tournament.id, round_number=4,
                                 stage="playoff")
        session.add(round_)
        await session.flush()
        session.add(TournamentMatch(round_id=round_.id,
                                    team_a_participant_id=alpha.id,
                                    team_b_participant_id=None, is_bye=True))
        await session.commit()
        round_id = round_.id

    cog = TournamentCog.__new__(TournamentCog)
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock())

    with patch("cogs.tournament_commands.db_session", _fake_db_session(test_db)):
        await cog._post_round_messages(channel, round_id, 4)

    texts = [c.args[0] for c in channel.send.call_args_list]
    bye_line = next(t for t in texts if "Alpha" in t)
    assert "auto win" not in bye_line
    assert "no match" in bye_line
