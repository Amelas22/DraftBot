import pytest
from unittest.mock import AsyncMock, patch

from models.tournament import STAGE_PLAY_IN, TournamentMatch
from services.tournament_service import _playoff_rounds, get_final_placement, set_result, start_playoff
from tournament_fixtures import _matches, _participants, _swiss_done


@pytest.mark.asyncio
async def test_the_play_in_feeds_the_last_seed_slot(session):
    t = await _swiss_done(session, cut_to=4, teams=6)
    parts = await _participants(session, t.id)              # best first
    await start_playoff(session, t.id, play_in=(parts[3].id, parts[4].id))
    rounds = await _playoff_rounds(session, t.id)
    assert rounds[0].stage == STAGE_PLAY_IN
    play_in = (await _matches(session, rounds[0].id))[0]
    assert (play_in.team_a_participant_id, play_in.team_b_participant_id) == (parts[3].id, parts[4].id)
    parent = await session.get(TournamentMatch, play_in.feeds_match_id)
    assert parent.team_a_participant_id == parts[0].id and parent.team_b_participant_id is None
    assert [p.seed for p in parts[:5]] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_the_play_in_loser_places_right_below_the_bracket(session):
    t = await _swiss_done(session, cut_to=4, teams=6)
    parts = await _participants(session, t.id)
    await start_playoff(session, t.id, play_in=(parts[3].id, parts[4].id))
    play_in = (await _matches(session, (await _playoff_rounds(session, t.id))[0].id))[0]
    await set_result(session, play_in.id, 2, 0)
    placement = [p.id for p in await get_final_placement(session, t.id)]
    assert placement[4] == parts[4].id and placement[5] == parts[5].id


@pytest.mark.asyncio
async def test_a_play_in_needs_two_distinct_eligible_teams(session):
    t = await _swiss_done(session, cut_to=4, teams=6)
    parts = await _participants(session, t.id)
    with pytest.raises(ValueError, match="two different"):
        await start_playoff(session, t.id, play_in=(parts[3].id, parts[3].id))


@pytest.mark.asyncio
async def test_rooms_open_for_the_play_in_and_the_matches_not_waiting_on_it(match_control_db):
    from cogs import tournament_commands as tc
    from test_bracket_rooms import _bot, _fake_channel, _fake_db_session
    async with match_control_db() as session:
        t = await _swiss_done(session, cut_to=8, teams=10)
        parts = await _participants(session, t.id)
        await start_playoff(session, t.id, play_in=(parts[7].id, parts[8].id))
        t.bracket_channel_id = "55"
        t.guild_id = "42"
        tid = t.id
        rounds = await _playoff_rounds(session, tid)
        assert t.current_round == rounds[0].round_number
        await session.commit()
    channel, posted = _fake_channel()
    bot, _ = _bot(channel)
    with patch.object(tc, "db_session", _fake_db_session(match_control_db)), \
         patch.object(tc, "create_match_room", AsyncMock(return_value=None)):
        opened = await tc.open_ready_bracket_matches(bot, tid)
    assert len(opened) == 4          # the play-in plus the three quarterfinals that are not seed 1's
    assert sum("Play-in" in line for line in posted) == 1


@pytest.mark.asyncio
async def test_bracket_rows_list_the_play_in_first_and_the_final_last(session):
    from services.tournament_service import bracket_rows
    t = await _swiss_done(session, cut_to=4, teams=6)
    parts = await _participants(session, t.id)
    await start_playoff(session, t.id, play_in=(parts[3].id, parts[4].id))
    rows = await bracket_rows(session, t.id)
    assert rows[0].stage == "Play-in"
    assert rows[-1].stage == "Final"
