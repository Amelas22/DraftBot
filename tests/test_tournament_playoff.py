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


@pytest.mark.asyncio
async def test_final_placement_without_a_bracket_is_just_standings(session):
    """Non-cut tournaments must pay out exactly as they do today."""
    from services.tournament_service import get_final_placement, get_standings_data
    t = await _swiss_done(session, cut_to=None, teams=4)
    placement = await get_final_placement(session, t.id)
    standings = await get_standings_data(session, t.id)
    assert [p.id for p in placement] == [p.id for p in standings]


@pytest.mark.asyncio
async def test_the_bracket_winner_places_first_even_if_seeded_lower(session):
    """The whole point: paying the swiss leader after they lost the final
    would be wrong."""
    from services.tournament_service import get_final_placement, set_result
    t = await _swiss_done(session, cut_to=4, teams=4)
    first = await start_playoff(session, t.id)
    matches = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == first.id)
        .order_by(TournamentMatch.id)
    )).scalars().all()
    # Seed 4 upsets seed 1; seed 2 wins its match.
    await set_result(session, matches[0].id, 0, 2)
    await set_result(session, matches[1].id, 2, 0)
    final = await advance_round(session, t.id, random.Random(1))
    fm = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == final.id)
    )).scalars().first()
    await set_result(session, fm.id, 2, 0)       # the seed-4 team wins it all
    await advance_round(session, t.id, random.Random(1))

    placement = await get_final_placement(session, t.id)
    assert placement[0].seed == 4
    assert [p.seed for p in placement] == [4, 2, 1, 3]


@pytest.mark.asyncio
async def test_finish_tournament_returns_the_bracket_winner(session):
    """finish_tournament is the early-exit path and the champion announcement
    both read its return value -- neither may report the swiss leader once a
    bracket has been played."""
    from services.tournament_service import finish_tournament, set_result
    t = await _swiss_done(session, cut_to=4, teams=4)
    first = await start_playoff(session, t.id)
    matches = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == first.id)
        .order_by(TournamentMatch.id)
    )).scalars().all()
    await set_result(session, matches[0].id, 0, 2)     # seed 4 upsets seed 1
    await set_result(session, matches[1].id, 2, 0)
    final = await advance_round(session, t.id, random.Random(1))
    fm = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == final.id)
    )).scalars().first()
    await set_result(session, fm.id, 2, 0)

    t.status = "active"                                 # finish_tournament needs it active
    champion = await finish_tournament(session, t.id)
    assert champion.seed == 4


@pytest.mark.asyncio
async def test_a_partially_reported_bracket_ranks_live_teams_above_eliminated_ones(session):
    """Regression: a tournament can be `finish`ed (or paid out) with the
    bracket mid-stream -- /tournament finish performs no unreported-match
    check. A team still alive in an unreported later round must never rank
    below a team the bracket has already eliminated in an earlier one."""
    from services.tournament_service import get_final_placement, set_result
    t = await _swiss_done(session, cut_to=4, teams=4)
    first = await start_playoff(session, t.id)
    matches = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == first.id)
        .order_by(TournamentMatch.id)
    )).scalars().all()
    await set_result(session, matches[0].id, 0, 2)     # seed 4 upsets seed 1
    await set_result(session, matches[1].id, 0, 2)     # seed 3 upsets seed 2
    await advance_round(session, t.id, random.Random(1))  # final created, NOT reported

    placement = await get_final_placement(session, t.id)
    # The two live finalists (seed 3, seed 4) rank above the two teams the
    # bracket has already eliminated (seed 1, seed 2); ties within each
    # group break by seed.
    assert [p.seed for p in placement] == [3, 4, 1, 2]


@pytest.mark.asyncio
async def test_teams_that_missed_the_cut_place_below_every_bracket_team_in_swiss_order(session):
    """The bracket only covers the cut; teams that missed it must still be
    ranked -- below every bracket team, in swiss order."""
    from services.tournament_service import get_final_placement, get_standings_data
    t = await _swiss_done(session, cut_to=4, teams=6)
    first = await start_playoff(session, t.id)
    await _report_all(session, first.id)
    final = await advance_round(session, t.id, random.Random(1))
    await _report_all(session, final.id)
    await advance_round(session, t.id, random.Random(1))

    placement = await get_final_placement(session, t.id)
    standings = await get_standings_data(session, t.id)
    missed_cut_ids_in_swiss_order = [p.id for p in standings if p.seed is None]
    tail = placement[len(placement) - len(missed_cut_ids_in_swiss_order):]
    assert [p.id for p in tail] == missed_cut_ids_in_swiss_order
    assert all(p.seed is None for p in tail)
    head = placement[:len(placement) - len(tail)]
    assert all(p.seed is not None for p in head)


@pytest.mark.asyncio
async def test_payout_allocations_follow_bracket_placement_not_swiss_order(session):
    """Design spec: payout allocations follow bracket placement, not swiss
    order. compute_allocations is pure, so feed it get_final_placement's
    output from a tournament where the swiss leader lost the final, and
    assert the winner's share goes to whoever actually won the bracket."""
    from services.tournament_service import get_final_placement, set_result
    from services.tournament_escrow_service import compute_allocations
    t = await _swiss_done(session, cut_to=4, teams=4)
    first = await start_playoff(session, t.id)
    matches = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == first.id)
        .order_by(TournamentMatch.id)
    )).scalars().all()
    await set_result(session, matches[0].id, 0, 2)     # seed 4 upsets seed 1
    await set_result(session, matches[1].id, 2, 0)
    final = await advance_round(session, t.id, random.Random(1))
    fm = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == final.id)
    )).scalars().first()
    await set_result(session, fm.id, 2, 0)             # seed 4 wins it all
    await advance_round(session, t.id, random.Random(1))

    placement = await get_final_placement(session, t.id)
    assert placement[0].seed == 4                      # not the swiss leader (seed 1)
    ranked = [(p.captain_user_id, p.team_name) for p in placement if p.status == "paid"]
    allocations = compute_allocations(1000, "winner_take_all", ranked)
    assert allocations == [(1, placement[0].captain_user_id, placement[0].team_name, 1000)]


async def _played_swiss(session, cut_to=4):
    """A COMPLETE 4-team round-robin swiss with real, reported matches.

    Rigged (found by brute force over the 64 possible result sets) so that
    three teams tie on 3 points and OMW% -- the first tiebreak -- is what
    separates them. That is what makes the standings order sensitive to which
    matches are in the opponent graph.
    """
    from services.tournament_service import set_result
    t = await create_tournament(session, "g_omw", "OMW Test", 3, cut_to=cut_to)
    teams = {}
    for name in ("AA", "BB", "CC", "DD"):
        participant, _ = await register_team(session, t.id, name, f"cap{name}")
        participant.status = "paid"
        teams[name] = participant
    t.status = "active"
    schedule = [
        [("AA", "BB", 2, 0), ("CC", "DD", 2, 0)],
        [("AA", "CC", 2, 0), ("BB", "DD", 0, 2)],
        [("AA", "DD", 2, 0), ("BB", "CC", 2, 0)],
    ]
    for number, pairs in enumerate(schedule, start=1):
        rnd = TournamentRound(tournament_id=t.id, round_number=number, stage="swiss")
        session.add(rnd)
        await session.flush()
        for name_a, name_b, wins_a, wins_b in pairs:
            match = TournamentMatch(
                round_id=rnd.id,
                team_a_participant_id=teams[name_a].id,
                team_b_participant_id=teams[name_b].id,
            )
            session.add(match)
            await session.flush()
            await set_result(session, match.id, wins_a, wins_b)
    t.current_round = 3
    await session.flush()
    return t, teams


@pytest.mark.asyncio
async def test_the_bracket_does_not_reorder_frozen_standings(session):
    """The freeze covers OMW%, not just records. OMW% is the FIRST tiebreak and
    is computed from the opponent graph, so a bracket pairing in that graph
    reorders tied teams the instant the bracket is built -- before a single
    playoff game is played -- and the standings message then contradicts the
    seeds just announced."""
    from services.tournament_service import get_standings_data, set_result
    t, _ = await _played_swiss(session, cut_to=4)

    before = [p.team_name for p in await get_standings_data(session, t.id)]
    # The premise: without a points tie there is nothing for OMW% to reorder.
    assert len({p.points for p in await get_standings_data(session, t.id)}) < 4

    await start_playoff(session, t.id)
    after = [p.team_name for p in await get_standings_data(session, t.id)]
    assert after == before

    # And it still holds once bracket results exist.
    first = (await session.execute(
        select(TournamentRound).where(TournamentRound.tournament_id == t.id)
        .where(TournamentRound.stage == "playoff")
    )).scalars().first()
    for m in (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == first.id)
    )).scalars().all():
        await set_result(session, m.id, 2, 0)
    assert [p.team_name for p in await get_standings_data(session, t.id)] == before


@pytest.mark.asyncio
async def test_start_playoff_refuses_while_a_swiss_match_is_unreported(session):
    """advance_round refuses to move on with results outstanding; the explicit
    command must too. Otherwise seeds -- and the money that follows them -- are
    stamped from partial standings."""
    t = await _swiss_done(session, cut_to=4, teams=6)
    rounds = (await session.execute(
        select(TournamentRound).where(TournamentRound.tournament_id == t.id)
        .order_by(TournamentRound.round_number)
    )).scalars().all()
    parts = await _participants(session, t.id)
    session.add(TournamentMatch(
        round_id=rounds[-1].id,
        team_a_participant_id=parts[0].id,
        team_b_participant_id=parts[1].id,
    ))
    await session.flush()

    with pytest.raises(ValueError, match="need results"):
        await start_playoff(session, t.id)


@pytest.mark.asyncio
async def test_start_playoff_refuses_a_non_swiss_format(session):
    """A cut is defined off swiss standings. round_robin and manual set
    current_round = total_rounds at START, so the "swiss isn't finished" gate
    passes immediately -- /tournament playoff typed right after /tournament
    start would seed a bracket over a field that has played nothing."""
    t = await _swiss_done(session, cut_to=4, teams=6)
    t.format = "round_robin"
    await session.flush()

    with pytest.raises(ValueError, match="Swiss standings"):
        await start_playoff(session, t.id)


@pytest.mark.asyncio
async def test_a_decided_playoff_round_cannot_be_rewritten(session):
    """A bracket round closes the moment the next one is paired: its winners
    are already playing on. Reachable via record_linked_result, which writes to
    any match id -- the "a draft finishes after an admin ruling" case. Without
    the guard, correcting an advanced semifinal makes its loser champion
    without playing the final."""
    from services.tournament_service import get_final_placement, set_result
    t = await _swiss_done(session, cut_to=4, teams=4)
    first = await start_playoff(session, t.id)
    matches = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == first.id)
        .order_by(TournamentMatch.id)
    )).scalars().all()
    await set_result(session, matches[0].id, 2, 0)      # seed 1 beats seed 4
    await set_result(session, matches[1].id, 2, 0)      # seed 2 beats seed 3
    final = await advance_round(session, t.id, random.Random(1))
    fm = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == final.id)
    )).scalars().first()
    await set_result(session, fm.id, 2, 0)              # seed 1 wins it all
    assert [p.seed for p in await get_final_placement(session, t.id)] == [1, 2, 3, 4]

    with pytest.raises(ValueError, match="already decided"):
        await set_result(session, matches[0].id, 0, 2)  # "actually, 4 beat 1"

    # The bracket -- and the payout order read off it -- is unchanged.
    assert [p.seed for p in await get_final_placement(session, t.id)] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_the_latest_playoff_round_can_still_be_corrected(session):
    """The guard closes DECIDED rounds only: the round currently being played
    has no later round depending on it, so an admin typo there must still be
    fixable."""
    from services.tournament_service import set_result
    t = await _swiss_done(session, cut_to=4, teams=4)
    first = await start_playoff(session, t.id)
    match = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == first.id)
        .order_by(TournamentMatch.id)
    )).scalars().first()
    await set_result(session, match.id, 2, 0)
    await set_result(session, match.id, 0, 2)           # corrected, no next round yet
    assert (match.team_a_wins, match.team_b_wins) == (0, 2)


@pytest.mark.asyncio
async def test_a_bracket_bye_is_not_described_as_an_auto_win(session):
    """A swiss bye is a result (points awarded); a bracket bye is the absence
    of a match. The refusal must not tell an organizer the bracket bye was
    'scored automatically' -- nothing is scored there."""
    from services.tournament_service import set_result
    t = await _swiss_done(session, cut_to=6, teams=6)
    round_ = await start_playoff(session, t.id)
    bye = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == round_.id)
        .where(TournamentMatch.is_bye.is_(True))
    )).scalars().first()

    with pytest.raises(ValueError) as exc:
        await set_result(session, bye.id, 2, 0)
    assert "scored automatically" not in str(exc.value)
    assert "no match to report" in str(exc.value)
