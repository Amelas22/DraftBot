"""Top-N cut: starting the bracket, freezing swiss, advancing, placement."""
import os
import random
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
from models.tournament import TournamentMatch, TournamentParticipant, TournamentRound
from services.tournament_service import (
    create_tournament,
    register_team,
    start_playoff,
)


@pytest_asyncio.fixture
async def test_db():
    """Temporary database + session factory. Same shape as the fixture in
    tests/test_tournament_service.py, which yields a FACTORY, not a session."""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()
    os.unlink(temp_db.name)


@pytest_asyncio.fixture
async def session(test_db):
    """One open session per test — every test below wants a session, not a
    factory, and unwrapping it once here keeps the tests about the bracket."""
    async with test_db() as open_session:
        yield open_session


async def _swiss_done(session, cut_to=4, teams=6):
    """A tournament sitting at the end of swiss, with distinct points so the
    seeding order is unambiguous.

    Order matters: register_team refuses a tournament that is not open for
    registration, so teams go in FIRST and the status flips afterwards. It
    also returns (participant, created), not a participant.
    """
    t = await create_tournament(session, "g1", "Cut Test", 3)
    for i in range(teams):
        participant, _ = await register_team(session, t.id, f"Team{i}", f"cap{i}")
        participant.status = "paid"
        participant.points = (teams - i) * 3      # Team0 highest
    # A tournament at the end of swiss HAS its round rows; without them
    # _current_round returns None and the tests traverse a branch production
    # never reaches.
    for number in range(1, 4):
        session.add(TournamentRound(
            tournament_id=t.id, round_number=number, stage="swiss"))
    t.cut_to = cut_to
    t.status = "active"
    t.current_round = 3
    await session.flush()
    return t


async def _participants(session, tournament_id):
    return (await session.execute(
        select(TournamentParticipant).where(
            TournamentParticipant.tournament_id == tournament_id)
    )).scalars().all()


@pytest.mark.asyncio
async def test_start_playoff_stamps_seeds_and_builds_the_first_round(session):
    t = await _swiss_done(session, cut_to=4, teams=6)
    round_ = await start_playoff(session, t.id)

    assert round_.stage == "playoff"
    assert round_.round_number == t.total_rounds + 1

    seeded = (await session.execute(
        select(TournamentParticipant)
        .where(TournamentParticipant.tournament_id == t.id)
        .where(TournamentParticipant.seed.isnot(None))
    )).scalars().all()
    assert sorted(p.seed for p in seeded) == [1, 2, 3, 4]   # only the cut teams

    matches = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == round_.id)
    )).scalars().all()
    assert len(matches) == 2                                 # 1v4 and 2v3


@pytest.mark.asyncio
async def test_start_playoff_gives_top_seeds_byes_when_not_a_power_of_two(session):
    t = await _swiss_done(session, cut_to=6, teams=6)
    round_ = await start_playoff(session, t.id)
    matches = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == round_.id)
    )).scalars().all()
    byes = [m for m in matches if m.is_bye]
    assert len(byes) == 2
    seeds = {}
    for m in byes:
        p = await session.get(TournamentParticipant, m.team_a_participant_id)
        seeds[p.seed] = True
    assert set(seeds) == {1, 2}


@pytest.mark.asyncio
async def test_a_bracket_bye_awards_no_points(session):
    """Swiss is frozen at the cut, so a top seed must not gain points for a
    structural bye the way a swiss bye does."""
    t = await _swiss_done(session, cut_to=6, teams=6)
    before = {p.id: p.points for p in await _participants(session, t.id)}
    await start_playoff(session, t.id)
    after = {p.id: p.points for p in await _participants(session, t.id)}
    assert before == after


@pytest.mark.asyncio
async def test_start_playoff_refuses_a_short_field(session):
    t = await _swiss_done(session, cut_to=8, teams=6)
    with pytest.raises(ValueError, match="6"):
        await start_playoff(session, t.id)


@pytest.mark.asyncio
async def test_start_playoff_refuses_before_swiss_ends(session):
    t = await _swiss_done(session, cut_to=4, teams=6)
    t.current_round = 2
    await session.flush()
    with pytest.raises(ValueError, match="Swiss"):
        await start_playoff(session, t.id)


@pytest.mark.asyncio
async def test_start_playoff_refuses_twice(session):
    t = await _swiss_done(session, cut_to=4, teams=6)
    await start_playoff(session, t.id)
    with pytest.raises(ValueError, match="already"):
        await start_playoff(session, t.id)


@pytest.mark.asyncio
async def test_start_playoff_needs_a_size(session):
    t = await _swiss_done(session, cut_to=None, teams=6)
    with pytest.raises(ValueError, match="cut size"):
        await start_playoff(session, t.id)


@pytest.mark.asyncio
async def test_a_playoff_result_does_not_move_swiss_records(session):
    """The freeze. A team that went 3-0 in swiss still reads 3-0 after losing
    in the bracket -- otherwise the standings that produced the seeding become
    unrecoverable and OMW% is polluted by bracket matches."""
    from services.tournament_service import set_result
    t = await _swiss_done(session, cut_to=4, teams=6)
    round_ = await start_playoff(session, t.id)
    match = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == round_.id)
    )).scalars().first()

    before = {p.id: (p.points, p.match_wins, p.match_losses, p.game_wins)
              for p in await _participants(session, t.id)}
    await set_result(session, match.id, 2, 0)
    after = {p.id: (p.points, p.match_wins, p.match_losses, p.game_wins)
             for p in await _participants(session, t.id)}

    assert before == after
    assert match.team_a_wins == 2 and match.team_b_wins == 0   # still recorded


@pytest.mark.asyncio
async def test_a_swiss_result_still_moves_records(session):
    """Guard against the freeze leaking into swiss."""
    from services.tournament_service import set_result, start_tournament
    t = await create_tournament(session, "g2", "Normal", 2)
    for i in range(4):
        participant, _ = await register_team(session, t.id, f"T{i}", f"c{i}")
        participant.status = "paid"
    await start_tournament(session, t.id, random.Random(1))
    round_ = (await session.execute(
        select(TournamentRound).where(TournamentRound.tournament_id == t.id)
    )).scalars().first()
    match = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == round_.id)
    )).scalars().first()
    winner = await session.get(TournamentParticipant, match.team_a_participant_id)
    before = winner.points
    await set_result(session, match.id, 2, 0)
    assert winner.points > before


from services.tournament_service import SwissComplete, advance_round
import random


async def _report_all(session, round_id):
    """Report 2-0 to team A for every playable match in a round."""
    from services.tournament_service import set_result
    matches = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == round_id)
    )).scalars().all()
    for m in matches:
        if not m.is_bye:
            await set_result(session, m.id, 2, 0)
    return matches


@pytest.mark.asyncio
async def test_advance_round_pairs_the_bracket_winners(session):
    t = await _swiss_done(session, cut_to=4, teams=6)
    first = await start_playoff(session, t.id)
    reported = await _report_all(session, first.id)
    # _report_all always scores team A 2-0, so team A is the winner of record
    # for every match it touches.
    winners = {m.team_a_participant_id for m in reported}

    second = await advance_round(session, t.id, random.Random(1))
    assert second.stage == "playoff"
    matches = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == second.id)
    )).scalars().all()
    assert len(matches) == 1                     # the final
    final = matches[0]
    # Not just "a match exists" -- the two teams IN it must be the two
    # reported winners, not the losers.
    assert {final.team_a_participant_id, final.team_b_participant_id} == winners


@pytest.mark.asyncio
async def test_advance_round_carries_a_bye_team_into_the_next_round(session):
    """The bye branch in _advance_playoff: a team that sat out round 1 on a
    bye must still appear in round 2, paired against a real winner -- not
    dropped, and not paired against the other bye team."""
    t = await _swiss_done(session, cut_to=6, teams=6)
    first = await start_playoff(session, t.id)
    first_matches = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == first.id)
    )).scalars().all()
    bye_team_ids = {m.team_a_participant_id for m in first_matches if m.is_bye}
    assert len(bye_team_ids) == 2

    reported = await _report_all(session, first.id)
    winner_ids = {m.team_a_participant_id for m in reported if not m.is_bye}

    second = await advance_round(session, t.id, random.Random(1))
    second_matches = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == second.id)
    )).scalars().all()
    assert len(second_matches) == 2
    for m in second_matches:
        pair = {m.team_a_participant_id, m.team_b_participant_id}
        assert len(pair & bye_team_ids) == 1     # exactly one bye team...
        assert len(pair & winner_ids) == 1       # ...paired against one real winner


@pytest.mark.asyncio
async def test_a_drawn_playoff_match_refuses_to_advance(session):
    """Single elimination has no drawn match. set_result permits equal wins
    (an admin typo, or a genuine Bo3 draw), so silently crowning team B would
    hand the bracket to the wrong team with zero signal -- this must raise
    instead, and must not create the next round."""
    from services.tournament_service import _playoff_rounds, set_result
    t = await _swiss_done(session, cut_to=4, teams=6)
    first = await start_playoff(session, t.id)
    matches = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == first.id)
    )).scalars().all()
    non_byes = [m for m in matches if not m.is_bye]
    await set_result(session, non_byes[0].id, 1, 1)             # the draw
    await set_result(session, non_byes[1].id, 2, 0)

    with pytest.raises(ValueError, match="draw"):
        await advance_round(session, t.id, random.Random(1))

    rounds_after = await _playoff_rounds(session, t.id)
    assert len(rounds_after) == 1                # no new round was created


@pytest.mark.asyncio
async def test_the_final_completes_the_tournament(session):
    t = await _swiss_done(session, cut_to=4, teams=6)
    first = await start_playoff(session, t.id)
    await _report_all(session, first.id)
    final = await advance_round(session, t.id, random.Random(1))
    await _report_all(session, final.id)

    assert await advance_round(session, t.id, random.Random(1)) is None
    assert t.status == "completed"


@pytest.mark.asyncio
async def test_end_of_swiss_with_a_cut_pending_asks_instead_of_completing(session):
    """Completing is irreversible and one call away; with a cut declared the
    caller must be given the choice rather than have it made for them."""
    t = await _swiss_done(session, cut_to=4, teams=6)
    with pytest.raises(SwissComplete) as exc:
        await advance_round(session, t.id, random.Random(1))
    assert exc.value.cut_to == 4
    assert exc.value.eligible == 6
    assert t.status == "active"                  # NOT completed
