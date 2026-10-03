"""The `match` autocomplete of /tournament set_result: the active tournament's
reportable matches, named, so ids are discoverable without a pairing line."""
import pytest
import pytest_asyncio

from models.tournament import TournamentMatch, TournamentRound
from services.tournament_service import (
    create_tournament,
    register_team,
    reportable_match_choices,
)


@pytest_asyncio.fixture
async def session(match_control_db):
    async with match_control_db() as open_session:
        yield open_session


async def _event(session, guild="g1", name="Cup", status="active"):
    t = await create_tournament(session, guild, name, 3)
    teams = []
    for i in range(4):
        p, _ = await register_team(session, t.id, f"{name}{i}", f"{guild}{name}{i}")
        p.status = "paid"
        teams.append(p)
    t.status = status
    await session.flush()
    return t, teams


async def _round(session, t, number, stage="swiss"):
    r = TournamentRound(tournament_id=t.id, round_number=number, stage=stage)
    session.add(r)
    await session.flush()
    return r


async def _match(session, r, a, b, wins=None, bye=False):
    m = TournamentMatch(
        round_id=r.id, team_a_participant_id=a.id, team_b_participant_id=b.id,
        is_bye=bye,
        team_a_wins=None if wins is None else wins[0],
        team_b_wins=None if wins is None else wins[1])
    session.add(m)
    await session.flush()
    return m


@pytest.mark.asyncio
async def test_unreported_first_then_most_recent_with_labels(session):
    t, (a, b, c, d) = await _event(session)
    r1 = await _round(session, t, 1)
    old_done = await _match(session, r1, a, b, wins=(2, 0))
    old_open = await _match(session, r1, c, d)
    r2 = await _round(session, t, 2)
    new_done = await _match(session, r2, a, c, wins=(2, 1))
    new_open = await _match(session, r2, b, d)

    rows = await reportable_match_choices(session, "g1")

    assert [m for m, _ in rows] == [new_open.id, old_open.id, new_done.id, old_done.id]
    assert rows[0][1] == f"#{new_open.id} · Week 2 · Cup1 vs Cup3"


@pytest.mark.asyncio
async def test_byes_are_not_offered(session):
    t, (a, b, c, d) = await _event(session)
    r1 = await _round(session, t, 1)
    await _match(session, r1, a, a, bye=True)
    real = await _match(session, r1, b, c)

    assert [m for m, _ in await reportable_match_choices(session, "g1")] == [real.id]


@pytest.mark.asyncio
async def test_typed_text_filters_the_label(session):
    t, (a, b, c, d) = await _event(session)
    r1 = await _round(session, t, 1)
    await _match(session, r1, a, b)
    want = await _match(session, r1, c, d)

    rows = await reportable_match_choices(session, "g1", "cup3")

    assert [m for m, _ in rows] == [want.id]


@pytest.mark.asyncio
async def test_other_guild_and_completed_tournaments_are_excluded(session):
    done, (da, db_, _, _) = await _event(session, name="Old", status="completed")
    await _match(session, await _round(session, done, 1), da, db_)
    other, (oa, ob, _, _) = await _event(session, guild="g2", name="Other")
    await _match(session, await _round(session, other, 1), oa, ob)
    t, (a, b, c, d) = await _event(session)
    r1 = await _round(session, t, 1)
    mine = await _match(session, r1, a, b)

    assert [m for m, _ in await reportable_match_choices(session, "g1")] == [mine.id]
    assert await reportable_match_choices(session, "g3") == []


@pytest.mark.asyncio
async def test_swiss_matches_drop_out_once_a_bracket_exists(session):
    t, (a, b, c, d) = await _event(session)
    await _match(session, await _round(session, t, 1), a, b, wins=(2, 0))
    semi = await _round(session, t, 4, stage="playoff")
    m1 = await _match(session, semi, a, c)
    m2 = await _match(session, semi, b, d)

    rows = await reportable_match_choices(session, "g1")

    assert [m for m, _ in rows] == [m1.id, m2.id][::-1]
    assert all("Semifinal" in label for _, label in rows)


@pytest.mark.asyncio
async def test_capped_at_discords_limit(session):
    t, (a, b, c, d) = await _event(session)
    r1 = await _round(session, t, 1)
    for _ in range(30):
        await _match(session, r1, a, b)

    assert len(await reportable_match_choices(session, "g1")) == 25
