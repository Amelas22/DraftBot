"""Top-N cut: starting the bracket, freezing swiss, advancing, placement."""
from datetime import datetime
import random
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from models.draft_session import DraftSession
from models.tournament import STAGE_PLAY_IN, STAGE_SWISS, TournamentMatch, TournamentParticipant, TournamentRound
from services.tournament_escrow_service import compute_allocations
from services.tournament_service import (
    SwissComplete,
    _playoff_rounds,
    advance_round,
    create_tournament,
    finish_tournament,
    get_final_placement,
    get_standings_data,
    get_standings_with_omw,
    register_team,
    set_result,
    start_playoff,
    start_tournament,
    sync_linked_result,
)

from tournament_fixtures import _matches, _participants, _swiss_done


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

    matches = await _matches(session, round_.id)
    assert len(matches) == 2                                 # 1v4 and 2v3


@pytest.mark.asyncio
async def test_start_playoff_gives_top_seeds_byes_when_not_a_power_of_two(session):
    t = await _swiss_done(session, cut_to=6, teams=6)
    round_ = await start_playoff(session, t.id)
    matches = await _matches(session, round_.id)
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
    with pytest.raises(ValueError) as caught:
        await start_playoff(session, t.id)
    # Named counts, not a bare "6": the message has to say how many are
    # eligible AND what was asked for, or an organiser cannot tell whether to
    # shrink the cut or chase a drop.
    assert "6 eligible team(s)" in str(caught.value)
    assert "top 8" in str(caught.value)


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
    t = await _swiss_done(session, cut_to=4, teams=6)
    round_ = await start_playoff(session, t.id)
    match = (await _matches(session, round_.id))[0]

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
    t = await create_tournament(session, "g2", "Normal", 2)
    for i in range(4):
        participant, _ = await register_team(session, t.id, f"T{i}", f"c{i}")
        participant.status = "paid"
    await start_tournament(session, t.id, random.Random(1))
    round_ = (await session.execute(
        select(TournamentRound).where(TournamentRound.tournament_id == t.id)
    )).scalars().first()
    match = (await _matches(session, round_.id))[0]
    winner = await session.get(TournamentParticipant, match.team_a_participant_id)
    before = winner.points
    await set_result(session, match.id, 2, 0)
    assert winner.points > before


async def _report_all(session, round_id):
    """Report 2-0 to team A for every playable match in a round."""
    matches = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.round_id == round_id)
    )).scalars().all()
    for m in matches:
        if not m.is_bye:
            await set_result(session, m.id, 2, 0)
    return matches


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
    t = await _swiss_done(session, cut_to=None, teams=4)
    placement = await get_final_placement(session, t.id)
    standings = await get_standings_data(session, t.id)
    assert [p.id for p in placement] == [p.id for p in standings]


async def _final_match(session, tournament_id):
    rounds = await _playoff_rounds(session, tournament_id)
    return (await _matches(session, rounds[-1].id))[0]


async def _seed_four_wins_it_all(session):
    """A top-4 bracket played out to a REPORTED final that the 4 seed won:
    seed 4 upsets seed 1, seed 2 beats seed 3, seed 4 takes the final.

    Deciding the final completes the tournament; placement reads the same
    either way.
    """
    t = await _swiss_done(session, cut_to=4, teams=4)
    first = await start_playoff(session, t.id)
    matches = await _matches(session, first.id)
    await set_result(session, matches[0].id, 0, 2)     # seed 4 upsets seed 1
    await set_result(session, matches[1].id, 2, 0)     # seed 2 beats seed 3
    await set_result(session, (await _final_match(session, t.id)).id, 2, 0)
    return t


@pytest.mark.asyncio
async def test_the_bracket_winner_places_first_even_if_seeded_lower(session):
    """The whole point: paying the swiss leader after they lost the final
    would be wrong."""
    t = await _seed_four_wins_it_all(session)

    placement = await get_final_placement(session, t.id)
    assert placement[0].seed == 4
    assert [p.seed for p in placement] == [4, 2, 1, 3]


@pytest.mark.asyncio
async def test_finish_tournament_returns_the_bracket_winner(session):
    """finish_tournament is the early-exit path and the champion announcement
    both read its return value -- neither may report the swiss leader once a
    bracket has been played."""
    t = await _seed_four_wins_it_all(session)
    # Deciding the final completes the tournament itself; reopen it to
    # exercise finish_tournament's own champion lookup.
    t.status = "active"

    champion = await finish_tournament(session, t.id)
    assert champion.seed == 4


@pytest.mark.asyncio
async def test_a_partially_reported_bracket_ranks_live_teams_above_eliminated_ones(session):
    """Regression: a tournament can be `finish`ed (or paid out) with the
    bracket mid-stream -- /tournament finish performs no unreported-match
    check. A team still alive in an unreported later round must never rank
    below a team the bracket has already eliminated in an earlier one."""
    t = await _swiss_done(session, cut_to=4, teams=4)
    first = await start_playoff(session, t.id)
    matches = await _matches(session, first.id)
    await set_result(session, matches[0].id, 0, 2)     # seed 4 upsets seed 1
    await set_result(session, matches[1].id, 0, 2)     # seed 3 upsets seed 2
    # the final now holds both winners, NOT reported

    placement = await get_final_placement(session, t.id)
    # The two live finalists (seed 3, seed 4) rank above the two teams the
    # bracket has already eliminated (seed 1, seed 2); ties within each
    # group break by seed.
    assert [p.seed for p in placement] == [3, 4, 1, 2]


@pytest.mark.asyncio
async def test_teams_that_missed_the_cut_place_below_every_bracket_team_in_swiss_order(session):
    """The bracket only covers the cut; teams that missed it must still be
    ranked -- below every bracket team, in swiss order."""
    t = await _swiss_done(session, cut_to=4, teams=6)
    first = await start_playoff(session, t.id)
    await _report_all(session, first.id)
    await set_result(session, (await _final_match(session, t.id)).id, 2, 0)

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
    t = await _seed_four_wins_it_all(session)

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
    for m in await _matches(session, first.id):
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
async def test_a_bracket_bye_is_not_described_as_an_auto_win(session):
    """A swiss bye is a result (points awarded); a bracket bye is the absence
    of a match. The refusal must not tell an organizer the bracket bye was
    'scored automatically' -- nothing is scored there."""
    t = await _swiss_done(session, cut_to=6, teams=6)
    round_ = await start_playoff(session, t.id)
    bye = next(m for m in await _matches(session, round_.id) if m.is_bye)

    with pytest.raises(ValueError) as exc:
        await set_result(session, bye.id, 2, 0)
    assert "scored automatically" not in str(exc.value)
    assert "no match to report" in str(exc.value)


@pytest.mark.asyncio
async def test_final_placement_ranks_a_drawn_playoff_match_as_undecided(session):
    """A draw has no winner, and set_result already refuses to advance one. The
    placement copy of that decision once fell through to "team B won", crowning
    a team nobody beat."""
    t = await _swiss_done(session, cut_to=4, teams=4)
    first = await start_playoff(session, t.id)
    matches = await _matches(session, first.id)
    await set_result(session, matches[0].id, 2, 0)
    await set_result(session, matches[1].id, 2, 0)
    fm = await _final_match(session, t.id)
    await set_result(session, fm.id, 1, 1)          # an admin typo, or a real Bo3 draw

    # A draw is not a result: the placement skips it, so both teams read as
    # still alive and rank above everyone eliminated.
    placement = await get_final_placement(session, t.id)
    assert {p.id for p in placement[:2]} == {fm.team_a_participant_id,
                                             fm.team_b_participant_id}


@pytest.mark.asyncio
async def test_a_completed_tournaments_playoff_result_cannot_be_rewritten(session):
    """The closed-round guard only refuses a round with a LATER playoff round,
    and the final has none -- so it stayed rewritable forever. record_linked_result
    writes to any match id when a linked draft finishes, which is how a late draft
    could rewrite the champion of a tournament already announced and paid out."""
    t = await _swiss_done(session, cut_to=4, teams=4)
    first = await start_playoff(session, t.id)
    await _report_all(session, first.id)
    fm = await _final_match(session, t.id)
    await set_result(session, fm.id, 2, 0)
    assert t.status == "completed"
    champion_id = (await get_final_placement(session, t.id))[0].id

    with pytest.raises(ValueError, match="results are final"):
        await set_result(session, fm.id, 0, 2)      # "actually, the other team won"

    assert (fm.team_a_wins, fm.team_b_wins) == (2, 0)
    assert (await get_final_placement(session, t.id))[0].id == champion_id


@pytest.mark.asyncio
async def test_advance_round_refuses_a_current_round_with_no_round_row(session):
    """A tournament whose current_round points at a missing round row used to
    sail through the unreported-match check on an empty match list and complete
    itself -- irreversibly, and silently. Completing must never be the quiet
    branch."""
    t = await _swiss_done(session, cut_to=None, teams=4)
    await session.execute(
        delete(TournamentRound).where(TournamentRound.tournament_id == t.id))
    await session.flush()

    with pytest.raises(ValueError, match="no round row"):
        await advance_round(session, t.id, random.Random(1))

    assert t.status == "active"


@pytest.mark.asyncio
async def test_start_playoff_counts_dropped_teams_out_of_the_cut(session):
    """Six entered, two dropped, cut to 6: the bracket cannot be filled even
    though the tournament has six teams in it."""
    t = await _swiss_done(session, cut_to=6, teams=6)
    for participant in (await _participants(session, t.id))[:2]:
        participant.dropped_at = datetime.now()
    await session.flush()

    with pytest.raises(ValueError) as caught:
        await start_playoff(session, t.id)
    assert "4 eligible team(s)" in str(caught.value)


@pytest.mark.asyncio
async def test_the_prompt_and_the_bracket_agree_that_a_cut_is_unfillable(session):
    """The Start button's state IS what `start_playoff` will do.

    This is the invariant the shared rule exists for. The prompt used to
    decide for itself, comparing an eligible count against the cut size; if
    that ever disagreed with what `start_playoff` refuses, the organiser got a
    live button that then errored. Asserting the two together is the only test
    that catches them drifting apart.
    """
    t = await _swiss_done(session, cut_to=8, teams=6)
    with pytest.raises(SwissComplete) as prompt:
        await advance_round(session, t.id, random.Random(1))
    assert prompt.value.fillable is False
    with pytest.raises(ValueError):                   # ... and the bracket agrees
        await start_playoff(session, t.id)


@pytest.mark.asyncio
async def test_the_prompt_and_the_bracket_agree_that_a_cut_is_fillable(session):
    """The other half: an enabled button must lead to a bracket that starts."""
    t = await _swiss_done(session, cut_to=4, teams=6)
    with pytest.raises(SwissComplete) as prompt:
        await advance_round(session, t.id, random.Random(1))
    assert prompt.value.fillable is True
    assert await start_playoff(session, t.id) is not None


@pytest.mark.asyncio
async def test_start_playoff_seeds_past_a_dropped_team_rather_than_over_it(session):
    """A drop above the line moves the line down, it does not leave a hole.

    The refusal case is covered; this is the one that costs money if it is
    wrong. Eight teams, the top two dropped, cut to four: the seeds belong to
    the best four still IN, and no dropped team carries one.
    """
    t = await _swiss_done(session, cut_to=4, teams=8)
    ranked = await _participants(session, t.id)
    for participant in ranked[:2]:
        participant.dropped_at = datetime.now()
    await session.flush()

    await start_playoff(session, t.id)

    seeded = {p.team_name: p.seed for p in await _participants(session, t.id)
              if p.seed is not None}
    assert seeded == {"Team2": 1, "Team3": 2, "Team4": 3, "Team5": 4}



@pytest.mark.asyncio
async def test_a_play_in_match_does_not_affect_swiss_standings(session):
    """A play-in match is left out of OMW; counted, Alpha's OMW would be ~0.667, not 1/3."""
    t = await create_tournament(session, "g1", "Play-in Filter Test", 1)
    alpha, _ = await register_team(session, t.id, "Alpha", "cap_a")
    bravo, _ = await register_team(session, t.id, "Bravo", "cap_b")
    charlie, _ = await register_team(session, t.id, "Charlie", "cap_c")
    for p in [alpha, bravo, charlie]:
        p.status = "paid"
    await session.flush()

    # Create one Swiss round: Alpha beats Bravo
    r1 = TournamentRound(tournament_id=t.id, round_number=1, stage=STAGE_SWISS)
    session.add(r1)
    await session.flush()
    m1 = TournamentMatch(round_id=r1.id, team_a_participant_id=alpha.id,
                         team_b_participant_id=bravo.id, team_a_wins=1, team_b_wins=0)
    session.add(m1)
    alpha.match_wins = 1
    alpha.points = 3  # 1 win * 3 points
    bravo.match_losses = 1
    bravo.points = 0
    charlie.match_wins = 1  # 1 win, 0 losses
    charlie.points = 3  # 1 win * 3 points
    await session.flush()

    # Get OMW before play-in
    standings_before, omw_before = await get_standings_with_omw(session, t.id)
    omw_before_map = dict(omw_before)

    # Create play-in: Alpha vs Charlie
    playin = TournamentRound(tournament_id=t.id, round_number=2, stage=STAGE_PLAY_IN)
    session.add(playin)
    await session.flush()
    pm = TournamentMatch(round_id=playin.id, team_a_participant_id=alpha.id,
                         team_b_participant_id=charlie.id, team_a_wins=1, team_b_wins=0)
    session.add(pm)
    await session.flush()

    # Get OMW after play-in
    standings_after, omw_after = await get_standings_with_omw(session, t.id)
    omw_after_map = dict(omw_after)

    assert omw_before_map == omw_after_map, \
        f"Play-in match changed OMW: before {omw_before_map}, after {omw_after_map}"


async def _match_ids(session, tournament_id, stage="swiss"):
    return [m.id for m in (await session.execute(
        select(TournamentMatch).join(
            TournamentRound, TournamentMatch.round_id == TournamentRound.id)
        .where(TournamentRound.tournament_id == tournament_id,
               TournamentRound.stage == stage)
        .order_by(TournamentMatch.id))).scalars().all()]


@pytest.mark.asyncio
async def test_set_result_refuses_a_swiss_match_of_a_completed_tournament(session):
    t, _ = await _played_swiss(session, cut_to=None)
    match_id = (await _match_ids(session, t.id))[0]
    t.status = "completed"
    await session.flush()
    before = (await session.get(TournamentMatch, match_id)).team_a_wins

    with pytest.raises(ValueError, match="final"):
        await set_result(session, match_id, 0, 2)

    assert (await session.get(TournamentMatch, match_id)).team_a_wins == before


@pytest.mark.asyncio
async def test_set_result_refuses_a_swiss_match_once_the_cut_is_made(session):
    t, _ = await _played_swiss(session, cut_to=4)
    await start_playoff(session, t.id)
    match = await session.get(TournamentMatch, (await _match_ids(session, t.id))[0])
    before = (match.team_a_wins, match.team_b_wins)

    with pytest.raises(ValueError, match="froze at the cut"):
        await set_result(session, match.id, before[1], before[0])

    assert (match.team_a_wins, match.team_b_wins) == before


@pytest.mark.asyncio
async def test_set_result_still_corrects_a_swiss_match_mid_swiss(session):
    t, _ = await _played_swiss(session, cut_to=4)
    match_id = (await _match_ids(session, t.id))[0]

    match = await set_result(session, match_id, 0, 2)

    assert (match.team_a_wins, match.team_b_wins) == (0, 2)


@pytest.mark.asyncio
async def test_round_replies_name_bracket_rounds_by_match_count(session):
    """next_round and open_rooms reply with the round's name, not "Playoff
    round N"; the name needs the round's match count, so it is queried."""
    from services.tournament_formatter import round_name

    t, _ = await _played_swiss(session, cut_to=4)
    semi = await start_playoff(session, t.id)
    swiss_3 = TournamentRound(tournament_id=t.id, round_number=3, stage=STAGE_SWISS)

    assert await round_name(session, semi, 3, swiss_noun="Week") == "Semifinal"
    assert await round_name(session, swiss_3, 3, swiss_noun="Week") == "Week 3"


@pytest.mark.asyncio
async def test_the_cut_builds_every_bracket_round_at_once(session):
    t = await _swiss_done(session, cut_to=4, teams=6)
    await start_playoff(session, t.id)
    rounds = await _playoff_rounds(session, t.id)
    assert [len(await _matches(session, r.id)) for r in rounds] == [2, 1]
    final = (await _matches(session, rounds[1].id))[0]
    assert final.team_a_participant_id is None and final.team_b_participant_id is None
    semis = await _matches(session, rounds[0].id)
    assert [(m.feeds_match_id, m.feeds_slot) for m in semis] == [(final.id, "a"), (final.id, "b")]
    assert t.current_round == rounds[0].round_number


@pytest.mark.asyncio
async def test_a_bye_fills_its_parent_slot_at_the_cut(session):
    t = await _swiss_done(session, cut_to=6, teams=6)
    await start_playoff(session, t.id)
    rounds = await _playoff_rounds(session, t.id)
    first = await _matches(session, rounds[0].id)
    parts = await _participants(session, t.id)
    seed1 = parts[0].id
    bye = next(m for m in first if m.is_bye and m.team_a_participant_id == seed1)
    parent = await session.get(TournamentMatch, bye.feeds_match_id)
    slot = parent.team_a_participant_id if bye.feeds_slot == "a" else parent.team_b_participant_id
    assert slot == seed1


@pytest.mark.asyncio
async def test_bracket_match_ids_ascend_from_first_round_to_final(session):
    t = await _swiss_done(session, cut_to=8, teams=8)
    await start_playoff(session, t.id)
    ids = [m.id for r in await _playoff_rounds(session, t.id) for m in await _matches(session, r.id)]
    assert ids == sorted(ids)


async def _decide(session, match, a_wins=2, b_wins=1):
    return await set_result(session, match.id, a_wins, b_wins)


@pytest.mark.asyncio
async def test_a_result_moves_its_winner_into_the_parent_slot(session):
    t = await _swiss_done(session, cut_to=4, teams=4)
    await start_playoff(session, t.id)
    semis = await _matches(session, (await _playoff_rounds(session, t.id))[0].id)
    await _decide(session, semis[0])
    final = await session.get(TournamentMatch, semis[0].feeds_match_id)
    assert final.team_a_participant_id == semis[0].team_a_participant_id
    assert final.team_b_participant_id is None


@pytest.mark.asyncio
async def test_reporting_the_same_result_again_changes_nothing(session):
    t = await _swiss_done(session, cut_to=4, teams=4)
    await start_playoff(session, t.id)
    semis = await _matches(session, (await _playoff_rounds(session, t.id))[0].id)
    await _decide(session, semis[0])
    await _decide(session, semis[0])
    final = await session.get(TournamentMatch, semis[0].feeds_match_id)
    assert final.team_a_participant_id == semis[0].team_a_participant_id


@pytest.mark.asyncio
async def test_a_draw_is_recorded_but_advances_nobody(session):
    t = await _swiss_done(session, cut_to=4, teams=4)
    await start_playoff(session, t.id)
    semis = await _matches(session, (await _playoff_rounds(session, t.id))[0].id)
    await set_result(session, semis[0].id, 1, 1)
    final = await session.get(TournamentMatch, semis[0].feeds_match_id)
    assert (semis[0].team_a_wins, final.team_a_participant_id) == (1, None)


@pytest.mark.asyncio
async def test_a_winner_change_is_allowed_before_the_parent_room_opens(session):
    t = await _swiss_done(session, cut_to=4, teams=4)
    await start_playoff(session, t.id)
    semis = await _matches(session, (await _playoff_rounds(session, t.id))[0].id)
    await _decide(session, semis[0], 2, 1)
    await _decide(session, semis[0], 1, 2)
    final = await session.get(TournamentMatch, semis[0].feeds_match_id)
    assert final.team_a_participant_id == semis[0].team_b_participant_id


@pytest.mark.asyncio
async def test_a_winner_change_is_refused_once_the_parent_room_exists(session):
    t = await _swiss_done(session, cut_to=4, teams=4)
    await start_playoff(session, t.id)
    semis = await _matches(session, (await _playoff_rounds(session, t.id))[0].id)
    await _decide(session, semis[0], 2, 1)
    final = await session.get(TournamentMatch, semis[0].feeds_match_id)
    final.pairings_message_id = "123"            # its room has been posted
    with pytest.raises(ValueError, match="already open"):
        await _decide(session, semis[0], 1, 2)
    assert semis[0].team_a_wins == 2


@pytest.mark.asyncio
async def test_a_winner_change_is_refused_once_the_parent_is_decided(session):
    t = await _swiss_done(session, cut_to=4, teams=4)
    await start_playoff(session, t.id)
    rounds = await _playoff_rounds(session, t.id)
    semis = await _matches(session, rounds[0].id)
    for m in semis:
        await _decide(session, m)
    final = (await _matches(session, rounds[1].id))[0]
    final.team_a_wins, final.team_b_wins = 2, 0
    with pytest.raises(ValueError, match="already has a result"):
        await _decide(session, semis[0], 1, 2)


@pytest.mark.asyncio
async def test_score_change_with_same_winner_is_accepted_after_parent_room_opens(session):
    t = await _swiss_done(session, cut_to=4, teams=4)
    await start_playoff(session, t.id)
    semis = await _matches(session, (await _playoff_rounds(session, t.id))[0].id)
    await _decide(session, semis[0], 2, 0)
    final = await session.get(TournamentMatch, semis[0].feeds_match_id)
    final.pairings_message_id = "123"
    await _decide(session, semis[0], 3, 1)
    assert (semis[0].team_a_wins, semis[0].team_b_wins) == (3, 1)


@pytest.mark.asyncio
async def test_deciding_the_final_completes_the_tournament(session):
    t = await _swiss_done(session, cut_to=4, teams=4)
    await start_playoff(session, t.id)
    rounds = await _playoff_rounds(session, t.id)
    for m in await _matches(session, rounds[0].id):
        await _decide(session, m)
    final = (await _matches(session, rounds[1].id))[0]
    await _decide(session, final)
    assert t.status == "completed"


@pytest.mark.asyncio
async def test_a_match_still_waiting_for_its_teams_cannot_be_reported(session):
    t = await _swiss_done(session, cut_to=4, teams=4)
    await start_playoff(session, t.id)
    final = (await _matches(session, (await _playoff_rounds(session, t.id))[1].id))[0]
    with pytest.raises(ValueError, match="waiting for its teams"):
        await set_result(session, final.id, 2, 0)


@pytest.mark.asyncio
async def test_next_round_does_not_advance_a_bracket(session):
    t = await _swiss_done(session, cut_to=4, teams=4)
    await start_playoff(session, t.id)
    with pytest.raises(ValueError, match="advances on its own"):
        await advance_round(session, t.id, random.Random(1))


@pytest.mark.asyncio
async def test_a_bracket_parent_becoming_playable_moves_current_round(session):
    t = await _swiss_done(session, cut_to=4, teams=4)
    await start_playoff(session, t.id)
    rounds = await _playoff_rounds(session, t.id)
    for m in await _matches(session, rounds[0].id):
        await _decide(session, m)
    assert t.current_round == rounds[1].round_number


async def _two_decided_semis(session):
    t = await _swiss_done(session, cut_to=4, teams=4)
    await start_playoff(session, t.id)
    rounds = await _playoff_rounds(session, t.id)
    semis = await _matches(session, rounds[0].id)
    for m in semis:
        await _decide(session, m)                  # team A wins each
    final = (await _matches(session, rounds[1].id))[0]
    return t, semis, final


@pytest.mark.asyncio
async def test_a_relinked_final_keeps_both_teams_when_a_feeder_rescores(session):
    t, semis, final = await _two_decided_semis(session)
    w0, w1 = semis[0].team_a_participant_id, semis[1].team_a_participant_id
    # link_draft_to_match swaps a match's sides when the draft names come in reversed
    _swap_sides(final, semis)
    await _decide(session, semis[0], 3, 1)         # same winner, new score
    assert (final.team_a_participant_id, final.team_b_participant_id) == (w1, w0)


@pytest.mark.asyncio
async def test_a_winner_change_after_a_relink_replaces_the_right_team(session):
    t, semis, final = await _two_decided_semis(session)
    w0, w1 = semis[0].team_a_participant_id, semis[1].team_a_participant_id
    _swap_sides(final, semis)
    await _decide(session, semis[0], 1, 2)         # semi 0 flips
    assert (final.team_a_participant_id, final.team_b_participant_id) == (
        w1, semis[0].team_b_participant_id)
    final.pairings_message_id = "9"
    with pytest.raises(ValueError, match="already open"):
        await _decide(session, semis[0], 2, 1)


@pytest.mark.asyncio
async def test_a_thread_without_a_message_id_still_counts_as_an_open_room(session):
    t, semis, final = await _two_decided_semis(session)
    final.thread_id = "55"
    with pytest.raises(ValueError, match="already open"):
        await _decide(session, semis[0], 1, 2)


@pytest.mark.asyncio
async def test_a_decided_parent_refuses_a_winner_change_with_its_own_message(session):
    t, semis, final = await _two_decided_semis(session)
    final.team_a_wins, final.team_b_wins = 2, 0
    with pytest.raises(ValueError, match="already has a result"):
        await _decide(session, semis[0], 1, 2)


@pytest.mark.asyncio
async def test_a_draw_replacing_a_winner_clears_the_slot_until_the_room_opens(session):
    t, semis, final = await _two_decided_semis(session)
    await set_result(session, semis[0].id, 1, 1)
    assert final.team_a_participant_id is None
    await _decide(session, semis[0])               # decided again
    assert final.team_a_participant_id == semis[0].team_a_participant_id
    final.pairings_message_id = "9"
    with pytest.raises(ValueError, match="already open"):
        await set_result(session, semis[0].id, 1, 1)


@pytest.mark.asyncio
async def test_a_drawn_final_does_not_complete_the_tournament(session):
    t, semis, final = await _two_decided_semis(session)
    await set_result(session, final.id, 1, 1)
    assert t.status == "active"


@pytest.mark.asyncio
async def test_a_finished_finals_score_can_change_with_the_same_winner(session):
    t, semis, final = await _two_decided_semis(session)
    await _decide(session, final, 2, 0)
    assert t.status == "completed"
    await _decide(session, final, 3, 1)
    assert (final.team_a_wins, final.team_b_wins) == (3, 1)
    with pytest.raises(ValueError, match="results are final"):
        await _decide(session, final, 0, 2)
    with pytest.raises(ValueError, match="results are final"):
        await set_result(session, final.id, 1, 1)
    assert (final.team_a_wins, final.team_b_wins) == (3, 1)
    assert t.status == "completed"


@pytest.mark.asyncio
async def test_sync_follows_a_finished_finals_score_but_not_a_flip(session):
    t, semis, final = await _two_decided_semis(session)
    await _decide(session, final, 2, 0)

    @asynccontextmanager
    async def fake_db_session():
        yield session

    with patch("services.tournament_service.db_session", fake_db_session):
        assert await sync_linked_result(final.id, 3, 1) is not None
        assert (final.team_a_wins, final.team_b_wins) == (3, 1)
        assert await sync_linked_result(final.id, 1, 3) is None
    assert (final.team_a_wins, final.team_b_wins) == (3, 1)


@pytest.mark.asyncio
async def test_sync_still_ignores_a_swiss_match_of_a_finished_tournament(session):
    t = await _swiss_done(session, cut_to=None, teams=2)
    parts = await _participants(session, t.id)
    swiss = (await session.execute(select(TournamentRound).where(
        TournamentRound.tournament_id == t.id))).scalars().first()
    m = TournamentMatch(round_id=swiss.id, team_a_participant_id=parts[0].id,
                        team_b_participant_id=parts[1].id)
    session.add(m)
    await session.flush()
    await set_result(session, m.id, 2, 0)
    t.status = "completed"

    @asynccontextmanager
    async def fake_db_session():
        yield session

    with patch("services.tournament_service.db_session", fake_db_session):
        assert await sync_linked_result(m.id, 0, 2) is None
    assert (m.team_a_wins, m.team_b_wins) == (2, 0)


@pytest.mark.asyncio
async def test_a_cut_whose_byes_meet_makes_that_round_current(session):
    t = await _swiss_done(session, cut_to=5, teams=5)
    await start_playoff(session, t.id)
    rounds = await _playoff_rounds(session, t.id)
    # seeds 2 and 3 have byes into the same semifinal, which is playable now
    assert t.current_round == rounds[1].round_number


@pytest.mark.asyncio
async def test_a_draw_then_a_redecision_after_a_relink_keeps_the_sibling_winner(session):
    t, semis, final = await _two_decided_semis(session)
    w0, w1 = semis[0].team_a_participant_id, semis[1].team_a_participant_id
    _swap_sides(final, semis)
    await set_result(session, semis[0].id, 1, 1)
    assert (final.team_a_participant_id, final.team_b_participant_id) == (w1, None)
    await _decide(session, semis[0])
    assert (final.team_a_participant_id, final.team_b_participant_id) == (w1, w0)


@pytest.mark.asyncio
async def test_a_second_draw_after_a_relink_leaves_the_sibling_winner_alone(session):
    t, semis, final = await _two_decided_semis(session)
    w0, w1 = semis[0].team_a_participant_id, semis[1].team_a_participant_id
    _swap_sides(final, semis)
    await set_result(session, semis[0].id, 1, 1)
    await set_result(session, semis[0].id, 2, 2)
    assert (final.team_a_participant_id, final.team_b_participant_id) == (w1, None)


@pytest.mark.asyncio
async def test_a_cancelled_tournaments_bracket_scores_stay_frozen(session):
    t, semis, final = await _two_decided_semis(session)
    await _decide(session, final, 2, 0)
    t.status = "cancelled"
    with pytest.raises(ValueError, match="results are final"):
        await _decide(session, final, 3, 1)


def _swap_sides(final, semis):
    """What link_draft_to_match does when a draft names the teams reversed."""
    final.team_a_participant_id, final.team_b_participant_id = (
        final.team_b_participant_id, final.team_a_participant_id)
    for semi in semis:
        semi.feeds_slot = "b" if semi.feeds_slot == "a" else "a"


@pytest.mark.asyncio
async def test_a_second_feeder_drawn_after_a_relink_clears_its_own_slot(session):
    t, semis, final = await _two_decided_semis(session)
    w0, w1 = semis[0].team_a_participant_id, semis[1].team_a_participant_id
    _swap_sides(final, semis)
    await set_result(session, semis[0].id, 1, 1)
    await set_result(session, semis[1].id, 1, 1)
    assert (final.team_a_participant_id, final.team_b_participant_id) == (None, None)
    await _decide(session, semis[1], 1, 2)         # the other team goes through
    assert w1 not in (final.team_a_participant_id, final.team_b_participant_id)
    assert semis[1].team_b_participant_id in (final.team_a_participant_id,
                                              final.team_b_participant_id)


@pytest.mark.asyncio
async def test_the_mirror_order_of_draws_after_a_relink_is_also_safe(session):
    t, semis, final = await _two_decided_semis(session)
    _swap_sides(final, semis)
    await set_result(session, semis[1].id, 1, 1)
    await set_result(session, semis[0].id, 1, 1)
    await _decide(session, semis[0], 1, 2)
    assert semis[0].team_a_participant_id not in (final.team_a_participant_id,
                                                  final.team_b_participant_id)
    assert semis[0].team_b_participant_id in (final.team_a_participant_id,
                                              final.team_b_participant_id)


@pytest.mark.asyncio
async def test_placement_counts_decided_matches_in_an_unfinished_round(session):
    t = await _swiss_done(session, cut_to=4, teams=6)
    await start_playoff(session, t.id)
    semis = await _matches(session, (await _playoff_rounds(session, t.id))[0].id)
    await set_result(session, semis[0].id, 1, 2)          # seed 1 loses to seed 4
    placement = await get_final_placement(session, t.id)
    loser = semis[0].team_a_participant_id
    alive = {semis[0].team_b_participant_id, semis[1].team_a_participant_id,
             semis[1].team_b_participant_id}
    ids = [p.id for p in placement]
    assert set(ids[:3]) == alive
    assert ids[:3] == [semis[1].team_a_participant_id, semis[1].team_b_participant_id,
                       semis[0].team_b_participant_id]       # seeds 2, 3, 4
    assert ids[3] == loser                       # eliminated, but above the non-cut teams


@pytest.mark.asyncio
async def test_a_parent_with_a_linked_draft_refuses_a_winner_change(session):
    t, semis, final = await _two_decided_semis(session)
    session.add(DraftSession(session_id="linked", guild_id="g",
                             session_type="premade", tournament_match_id=final.id))
    await session.flush()
    before = (final.team_a_participant_id, final.team_b_participant_id)
    with pytest.raises(ValueError, match="linked draft"):
        await _decide(session, semis[0], 1, 2)
    assert (final.team_a_participant_id, final.team_b_participant_id) == before


@pytest.mark.asyncio
async def test_a_bye_team_that_then_loses_ranks_by_its_loss_depth(session):
    t = await _swiss_done(session, cut_to=3, teams=3)
    await start_playoff(session, t.id)
    rounds = await _playoff_rounds(session, t.id)
    first = await _matches(session, rounds[0].id)
    bye = next(m for m in first if m.is_bye)
    semi = next(m for m in first if not m.is_bye)
    await set_result(session, semi.id, 2, 0)
    final = (await _matches(session, rounds[1].id))[0]
    await set_result(session, final.id, 0, 2)       # the bye team (seat A) loses
    ids = [p.id for p in await get_final_placement(session, t.id)]
    assert ids[0] == semi.team_a_participant_id
    assert ids[1] == bye.team_a_participant_id
    assert ids[2] == semi.team_b_participant_id


@pytest.mark.asyncio
async def test_finish_refuses_while_the_final_is_drawn(session):
    t, semis, final = await _two_decided_semis(session)
    await set_result(session, final.id, 1, 1)
    with pytest.raises(ValueError, match=f"#{final.id} is drawn"):
        await finish_tournament(session, t.id)
    assert t.status == "active"


@pytest.mark.asyncio
async def test_finish_refuses_while_a_semi_is_drawn(session):
    t = await _swiss_done(session, cut_to=4, teams=4)
    await start_playoff(session, t.id)
    semis = await _matches(session, (await _playoff_rounds(session, t.id))[0].id)
    await set_result(session, semis[0].id, 1, 1)
    with pytest.raises(ValueError, match=f"#{semis[0].id} is drawn"):
        await finish_tournament(session, t.id)
    assert t.status == "active"


@pytest.mark.asyncio
async def test_finish_still_crowns_a_midstream_bracket_with_no_draw(session):
    t = await _swiss_done(session, cut_to=4, teams=4)
    await start_playoff(session, t.id)
    semis = await _matches(session, (await _playoff_rounds(session, t.id))[0].id)
    await set_result(session, semis[0].id, 2, 0)
    assert await finish_tournament(session, t.id) is not None
    assert t.status == "completed"
