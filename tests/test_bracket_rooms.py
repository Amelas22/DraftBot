"""Bracket matches get their rooms the moment both teams are known."""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tournament_fixtures import _matches, _swiss_done
from models.tournament import Tournament, TournamentMatch
from services.tournament_service import (
    _playoff_rounds, ready_bracket_matches, set_result, start_playoff,
)


@pytest.fixture(autouse=True)
def _fresh_locks():
    """Locks bind to the event loop that first used them; each test has its own."""
    from cogs import tournament_commands as tc
    tc._BRACKET_LOCKS.clear()


def _fake_db_session(factory):
    @asynccontextmanager
    async def fake():
        async with factory() as inner:
            yield inner
            await inner.commit()
    return fake


def _fake_channel():
    posted = []
    sent = {}
    channel = MagicMock()

    async def send(content):
        msg = MagicMock()
        msg.id = 1000 + len(posted)
        msg.channel.id = 55
        msg.edit = AsyncMock()
        posted.append(content)
        sent[msg.id] = msg
        return msg
    channel.send = send
    channel.fetch_message = AsyncMock(side_effect=lambda message_id: sent[message_id])
    return channel, posted


def _bot(channel):
    bot = MagicMock()
    bot.get_channel.return_value = channel
    bot.get_guild.return_value = MagicMock()
    cog = MagicMock()
    cog._drop_team_roles = AsyncMock()
    bot.get_cog.return_value = cog
    return bot, cog


async def _seeded(factory, cut_to=4, teams=4):
    async with factory() as session:
        t = await _swiss_done(session, cut_to=cut_to, teams=teams)
        await start_playoff(session, t.id)
        t.bracket_channel_id = "55"
        t.guild_id = "42"
        await session.commit()
        return t.id


@pytest.mark.asyncio
async def test_ready_matches_are_the_ones_with_both_teams_and_no_room(match_control_db):
    async with match_control_db() as session:
        t = await _swiss_done(session, cut_to=4, teams=4)
        await start_playoff(session, t.id)
        await session.commit()
        ready = await ready_bracket_matches(session, t.id)
        semis = await _matches(session, (await _playoff_rounds(session, t.id))[0].id)
        assert [r[0] for r in ready] == [m.id for m in semis]
        assert ready[0][3] == "Semifinal"


@pytest.mark.asyncio
async def test_concurrent_open_ready_posts_each_match_once(match_control_db):
    from cogs import tournament_commands as tc
    tid = await _seeded(match_control_db)
    channel, posted = _fake_channel()
    bot, _ = _bot(channel)
    with patch.object(tc, "db_session", _fake_db_session(match_control_db)), \
         patch.object(tc, "create_match_room", AsyncMock(return_value=None)), \
         patch.object(tc, "open_room_for_posted_match", AsyncMock(return_value=False)):
        await asyncio.gather(tc.open_ready_bracket_matches(bot, tid),
                             tc.open_ready_bracket_matches(bot, tid))
    assert len(posted) == 2
    assert all("· Semifinal" in line and "**#" in line for line in posted)


@pytest.mark.asyncio
async def test_startup_sweep_opens_rooms_for_ready_matches(match_control_db):
    from cogs import tournament_commands as tc
    async with match_control_db() as session:
        t = await _swiss_done(session, cut_to=4, teams=4)
        await start_playoff(session, t.id)
        t.bracket_channel_id = "55"
        semis = await _matches(session, (await _playoff_rounds(session, t.id))[0].id)
        for m in semis:
            m.pairings_message_id = "1"
            m.pairings_channel_id = "55"
            m.thread_id = "7"
            await set_result(session, m.id, 2, 0)     # final now ready, room never posted
        await session.commit()
    channel, posted = _fake_channel()
    bot, _ = _bot(channel)
    with patch.object(tc, "db_session", _fake_db_session(match_control_db)), \
         patch.object(tc, "create_match_room", AsyncMock(return_value=None)):
        await tc.sweep_brackets(bot)
    assert len(posted) == 1 and "· Final" in posted[0]


@pytest.mark.asyncio
async def test_meeting_byes_open_exactly_the_semifinal_they_fill(match_control_db):
    from cogs import tournament_commands as tc
    tid = await _seeded(match_control_db, cut_to=5, teams=5)
    channel, posted = _fake_channel()
    bot, _ = _bot(channel)
    async with match_control_db() as session:
        rounds = await _playoff_rounds(session, tid)
        expected = [m.id for r in rounds for m in await _matches(session, r.id)
                    if m.team_a_participant_id and m.team_b_participant_id
                    and not m.is_bye and m.team_a_wins is None]
        later = [m.id for m in await _matches(session, rounds[1].id)
                 if m.team_a_participant_id and m.team_b_participant_id]
    with patch.object(tc, "db_session", _fake_db_session(match_control_db)), \
         patch.object(tc, "create_match_room", AsyncMock(return_value=None)):
        opened = await tc.open_ready_bracket_matches(bot, tid)
    assert opened == expected
    assert later and later[0] in opened and len(opened) == 2
    assert len(posted) == 2 and any("· Semifinal" in line for line in posted)


async def _finish_semis(factory, tid, wins=(2, 0)):
    async with factory() as session:
        rounds = await _playoff_rounds(session, tid)
        for m in await _matches(session, rounds[0].id):
            await set_result(session, m.id, *wins)
        final = (await _matches(session, rounds[1].id))[0]
        await session.commit()
        return rounds, final.id


@pytest.mark.asyncio
async def test_only_the_completing_call_announces_the_champion(match_control_db):
    from cogs import tournament_commands as tc
    from services.tournament_service import record_result, sync_linked_result
    tid = await _seeded(match_control_db)
    channel, posted = _fake_channel()
    bot, cog = _bot(channel)
    _rounds, final_id = await _finish_semis(match_control_db, tid)
    with patch.object(tc, "db_session", _fake_db_session(match_control_db)), \
         patch.object(tc, "create_match_room", AsyncMock(return_value=None)), \
         patch.object(tc, "update_standings_message", AsyncMock()) as standings:
        async with match_control_db() as session:
            _m, completed_now = await record_result(session, final_id, 2, 1)
            await session.commit()
        assert completed_now is True
        await tc.after_bracket_result(bot, final_id, completed_now)
        # the draft re-reports the final's score after completion
        with patch("services.tournament_service.db_session",
                   _fake_db_session(match_control_db)):
            wrote = await sync_linked_result(final_id, 2, 0)
        assert wrote is not None and wrote[1] is False
        await tc.after_bracket_result(bot, final_id, wrote[1])
    champions = [p for p in posted if "is complete! Champion:" in p]
    assert len(champions) == 1 and "Team0" in champions[0]
    assert standings.await_count == 1
    assert cog._drop_team_roles.await_count == 1


@pytest.mark.asyncio
async def test_two_completions_report_completed_now_exactly_once(match_control_db):
    from services.tournament_service import record_result
    tid = await _seeded(match_control_db)
    _rounds, final_id = await _finish_semis(match_control_db, tid)
    async with match_control_db() as one, match_control_db() as two:
        _m, first = await record_result(one, final_id, 2, 1)
        await one.commit()
        _m, second = await record_result(two, final_id, 2, 0)
        await two.commit()
    assert (first, second) == (True, False)


@pytest.mark.asyncio
async def test_no_announcement_after_finish_then_a_semi_rereport(match_control_db):
    from cogs import tournament_commands as tc
    from services.tournament_service import finish_tournament, record_result
    tid = await _seeded(match_control_db)
    channel, posted = _fake_channel()
    bot, cog = _bot(channel)
    rounds, _final = await _finish_semis(match_control_db, tid)
    async with match_control_db() as session:
        await finish_tournament(session, tid)
        semi = (await _matches(session, rounds[0].id))[0]
        _m, completed_now = await record_result(session, semi.id, 2, 1)
        await session.commit()
        semi_id = semi.id
    assert completed_now is False
    with patch.object(tc, "db_session", _fake_db_session(match_control_db)), \
         patch.object(tc, "create_match_room", AsyncMock(return_value=None)), \
         patch.object(tc, "update_standings_message", AsyncMock()):
        await tc.after_bracket_result(bot, semi_id, completed_now)
    assert posted == [] and cog._drop_team_roles.await_count == 0


@pytest.mark.asyncio
async def test_no_rooms_in_a_closed_event(match_control_db):
    from cogs import tournament_commands as tc
    tid = await _seeded(match_control_db)
    async with match_control_db() as session:
        (await session.get(Tournament, tid)).status = "completed"
        await session.commit()
        assert await ready_bracket_matches(session, tid) == []
    channel, posted = _fake_channel()
    bot, _ = _bot(channel)
    with patch.object(tc, "db_session", _fake_db_session(match_control_db)), \
         patch.object(tc, "create_match_room", AsyncMock(return_value=None)):
        assert await tc.open_ready_bracket_matches(bot, tid) == []
    assert posted == []


@pytest.mark.asyncio
async def test_discord_failures_never_propagate_and_the_announcement_still_runs(match_control_db):
    import discord
    from cogs import tournament_commands as tc
    from services.tournament_service import record_result
    tid = await _seeded(match_control_db)
    _rounds, final_id = await _finish_semis(match_control_db, tid)
    async with match_control_db() as session:
        _m, completed_now = await record_result(session, final_id, 2, 1)
        await session.commit()
    bot, cog = _bot(MagicMock())
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    with patch.object(tc, "db_session", _fake_db_session(match_control_db)), \
         patch.object(tc, "update_standings_message", AsyncMock()) as standings:
        await tc.after_bracket_result(bot, final_id, completed_now)
    assert standings.await_count == 1 and cog._drop_team_roles.await_count == 1


@pytest.mark.asyncio
async def test_a_posted_line_whose_room_never_opened_is_repaired(match_control_db):
    from cogs import tournament_commands as tc
    tid = await _seeded(match_control_db)
    async with match_control_db() as session:
        rounds = await _playoff_rounds(session, tid)
        semis = await _matches(session, rounds[0].id)
        for m in semis:
            m.pairings_message_id = "9"
            m.pairings_channel_id = "55"
        semis[1].thread_id = "77"       # this one already has its room
        first = semis[0].id
        await session.commit()
    channel, posted = _fake_channel()
    message = MagicMock()
    message.edit = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=message)
    thread = MagicMock()
    thread.id = 321
    bot, _ = _bot(channel)
    room = AsyncMock(return_value=thread)
    with patch.object(tc, "db_session", _fake_db_session(match_control_db)), \
         patch.object(tc, "create_match_room", room):
        await tc.open_ready_bracket_matches(bot, tid)
    assert posted == []
    room.assert_awaited_once_with(message, first)
    assert "321" in message.edit.await_args.kwargs["content"]


@pytest.mark.asyncio
async def test_a_failed_write_after_posting_deletes_the_message():
    from cogs import tournament_commands as tc
    channel, _posted = _fake_channel()
    sent = MagicMock()
    sent.id, sent.channel.id = 5, 55
    sent.delete = AsyncMock()
    channel.send = AsyncMock(return_value=sent)

    @asynccontextmanager
    async def broken():
        raise RuntimeError("db down")
        yield
    with patch.object(tc, "db_session", broken):
        with pytest.raises(RuntimeError):
            await tc._post_match_room(MagicMock(), channel, 1, "A", "B", "Final")
    sent.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_swiss_match_is_a_noop_for_after_bracket_result(match_control_db):
    from cogs import tournament_commands as tc
    async with match_control_db() as session:
        t = await _swiss_done(session, cut_to=4, teams=4)
        from models.tournament import TournamentMatch, TournamentRound
        from sqlalchemy import select
        rnd = (await session.execute(select(TournamentRound))).scalars().first()
        m = TournamentMatch(round_id=rnd.id)
        session.add(m)
        await session.commit()
        mid = m.id
    channel, posted = _fake_channel()
    bot, cog = _bot(channel)
    with patch.object(tc, "db_session", _fake_db_session(match_control_db)):
        await tc.after_bracket_result(bot, mid)
    assert posted == [] and cog._drop_team_roles.await_count == 0


async def _race(factory, b_score):
    """B loaded the tournament as active before A completed it, then writes."""
    from services.tournament_service import record_result
    tid = await _seeded(factory)
    _rounds, final_id = await _finish_semis(factory, tid)
    async with factory() as one, factory() as two:
        # Held, not discarded: the identity map only keeps what is referenced.
        stale = (await two.get(Tournament, tid), await two.get(TournamentMatch, final_id))
        assert stale[0].status == "active"
        await record_result(one, final_id, 2, 1)           # A: Team0 wins
        await one.commit()
        try:
            _m, done = await record_result(two, final_id, *b_score)
            await two.commit()
            outcome = ("committed", done)
        except ValueError as e:
            outcome = ("refused", str(e))
    async with factory() as s:
        m = await s.get(TournamentMatch, final_id)
        stored = (m.team_a_wins, m.team_b_wins)
        status = (await s.get(Tournament, tid)).status
    return outcome, stored, status


@pytest.mark.asyncio
async def test_a_racing_result_cannot_flip_a_finished_final(match_control_db):
    outcome, stored, status = await _race(match_control_db, (0, 2))
    assert outcome[0] == "refused" and "final" in outcome[1]
    assert stored == (2, 1) and status == "completed"


@pytest.mark.asyncio
async def test_a_racing_same_winner_score_is_accepted_without_completing(match_control_db):
    outcome, stored, status = await _race(match_control_db, (2, 0))
    assert outcome == ("committed", False)
    assert stored == (2, 0) and status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("hold_final", [True, False])
async def test_a_stale_semifinal_rereport_cannot_change_a_finished_finals_teams(
        match_control_db, hold_final):
    from services.tournament_service import record_result
    tid = await _seeded(match_control_db)
    rounds, final_id = await _finish_semis(match_control_db, tid)
    async with match_control_db() as s:
        semi_id = (await _matches(s, rounds[0].id))[0].id
        f = await s.get(TournamentMatch, final_id)
        slots = (f.team_a_participant_id, f.team_b_participant_id)
    async with match_control_db() as one, match_control_db() as two:
        held = [await two.get(Tournament, tid), await two.get(TournamentMatch, semi_id)]
        if hold_final:
            held.append(await two.get(TournamentMatch, final_id))
        await record_result(one, final_id, 2, 1)
        await one.commit()
        with pytest.raises(ValueError, match="final"):
            await record_result(two, semi_id, 0, 2)
            await two.commit()
    async with match_control_db() as s:
        f = await s.get(TournamentMatch, final_id)
        assert (f.team_a_participant_id, f.team_b_participant_id) == slots
        assert (f.team_a_wins, f.team_b_wins) == (2, 1)
        assert (await s.get(Tournament, tid)).status == "completed"


@pytest.mark.asyncio
async def test_a_racing_draw_on_the_final_is_refused_after_completion(match_control_db):
    outcome, stored, status = await _race(match_control_db, (1, 1))
    assert outcome[0] == "refused"
    assert stored == (2, 1) and status == "completed"


from cogs.tournament_commands import _bracket_dm_text


def test_dm_names_the_result_and_what_it_opened():
    text = _bracket_dm_text("#143 Quarterfinal: CFB beat The initiative 5–3",
                            ["#146 Semifinal: CFB vs Big Trees"], None, False)
    assert "CFB beat The initiative 5–3" in text and "Opened #146 Semifinal" in text
    assert "republish" in text.lower()


def test_dm_for_a_draw_says_it_needs_settling():
    assert "needs settling" in _bracket_dm_text("#143 Quarterfinal: 2–2", [], None, True)


def test_dm_for_the_final_says_to_pay_out():
    text = _bracket_dm_text("#148 Final: CFB beat 🔥 5–4", [], "CFB", False)
    assert "Champion: CFB" in text and "/tournament payout" in text


def test_dm_lists_a_contained_failure():
    text = _bracket_dm_text("#143 Quarterfinal: A beat B 2–1", [], None, False,
                            ["couldn't open the rooms this result unlocked — run /tournament open_rooms"])
    assert "⚠️ couldn't open the rooms" in text and "/tournament open_rooms" in text


async def _semi_result_with_dm(match_control_db, dm_mock, create_room):
    from cogs import tournament_commands as tc
    from tournament_fixtures import _swiss_done
    async with match_control_db() as session:
        t = await _swiss_done(session, cut_to=4, teams=4)
        await start_playoff(session, t.id)
        t.guild_id, t.bracket_channel_id, t.organizer_user_id = "1", "55", "42"
        semis = await _matches(session, (await _playoff_rounds(session, t.id))[0].id)
        await set_result(session, semis[0].id, 2, 1)
        await session.commit()
        mid = semis[0].id
    channel, posted = _fake_channel()
    bot = MagicMock(); bot.get_channel.return_value = channel
    with patch.object(tc, "db_session", _fake_db_session(match_control_db)), \
         patch.object(tc, "create_match_room", create_room), \
         patch.object(tc, "send_dm", dm_mock):
        await tc.after_bracket_result(bot, mid)


@pytest.mark.asyncio
async def test_a_failed_dm_does_not_stop_the_bracket(match_control_db):
    dm = AsyncMock(return_value=False)
    await _semi_result_with_dm(match_control_db, dm, AsyncMock(return_value=None))
    dm.assert_awaited_once()
    assert "beat" in dm.call_args.args[2]


@pytest.mark.asyncio
async def test_the_dm_reports_a_step_that_failed(match_control_db):
    dm = AsyncMock(return_value=True)
    with patch("cogs.tournament_commands.open_ready_bracket_matches",
               AsyncMock(side_effect=RuntimeError("boom"))):
        await _semi_result_with_dm(match_control_db, dm, AsyncMock(return_value=None))
    assert "/tournament open_rooms" in dm.call_args.args[2]
