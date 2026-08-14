"""Tests for services/tournament_service.py (Slices 1-2)."""
import os
import random
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
from models.team import Team
from models.tournament import (
    Tournament,
    TournamentMatch,
    TournamentParticipant,
    TournamentRound,
    TournamentTeamMember,
)
from services.tournament_service import (
    add_match,
    add_teammate,
    advance_round,
    create_tournament,
    finish_tournament,
    find_current_match,
    find_participant_for_captain,
    get_active_tournament,
    get_rosters,
    get_standings_data,
    list_participants,
    register_team,
    remove_team,
    remove_teammate,
    set_result,
    start_tournament,
)


@pytest_asyncio.fixture
async def test_db():
    """Create a temporary test database and return a test session factory."""
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    test_session_factory = sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession
    )

    yield test_session_factory

    await engine.dispose()
    os.unlink(temp_db.name)


# ---- create_tournament / get_active_tournament -------------------------------

@pytest.mark.asyncio
async def test_create_tournament_opens_registration(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
    assert tournament.status == "registration"
    assert tournament.total_rounds == 3
    assert tournament.current_round == 0


@pytest.mark.asyncio
async def test_create_rejects_second_active_tournament_in_guild(test_db):
    async with test_db() as session:
        await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        with pytest.raises(ValueError):
            await create_tournament(session, "g1", "Summer", 3)


@pytest.mark.asyncio
async def test_create_allowed_in_other_guild_and_after_completion(test_db):
    async with test_db() as session:
        first = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()

        # Another guild is independent
        await create_tournament(session, "g2", "Spring", 3)
        await session.commit()

        # Completing the first frees the guild
        first.status = "completed"
        await session.commit()
        await create_tournament(session, "g1", "Summer", 3)
        await session.commit()


@pytest.mark.asyncio
async def test_get_active_tournament(test_db):
    async with test_db() as session:
        assert await get_active_tournament(session, "g1") is None
        created = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()

        active = await get_active_tournament(session, "g1")
        assert active is not None and active.id == created.id
        assert await get_active_tournament(session, "g2") is None

        created.status = "completed"
        await session.commit()
        assert await get_active_tournament(session, "g1") is None


# ---- register_team ------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_team_creates_team_and_participant(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()

        participant, created = await register_team(session, tournament.id, "Alpha", "42")
        await session.commit()

        assert created is True
        assert participant.team_name == "Alpha"
        assert participant.captain_user_id == "42"

        teams = (await session.execute(select(Team))).scalars().all()
        assert len(teams) == 1 and teams[0].TeamName == "Alpha"
        assert participant.team_id == teams[0].TeamID


@pytest.mark.asyncio
async def test_register_team_reuses_existing_team_case_insensitively(test_db):
    async with test_db() as session:
        session.add(Team(TeamName="Alpha"))
        await session.commit()

        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()

        participant, created = await register_team(session, tournament.id, "  alpha ", "42")
        await session.commit()

        assert created is True
        teams = (await session.execute(select(Team))).scalars().all()
        assert len(teams) == 1  # no duplicate team
        assert participant.team_name == "Alpha"  # canonical stored name


@pytest.mark.asyncio
async def test_register_team_is_idempotent(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()

        first, created_first = await register_team(session, tournament.id, "Alpha", "42")
        await session.commit()
        second, created_second = await register_team(session, tournament.id, "Alpha", "99")
        await session.commit()

        assert created_first is True and created_second is False
        assert second.id == first.id
        assert second.captain_user_id == "42"  # original captain kept

        participants = (await session.execute(select(TournamentParticipant))).scalars().all()
        assert len(participants) == 1


@pytest.mark.asyncio
async def test_register_team_rejected_outside_registration(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        tournament.status = "active"
        await session.commit()

        with pytest.raises(ValueError):
            await register_team(session, tournament.id, "Alpha", "42")


@pytest.mark.asyncio
async def test_register_team_rejects_unknown_tournament(test_db):
    async with test_db() as session:
        with pytest.raises(ValueError):
            await register_team(session, 999, "Alpha", "42")


# ---- remove_team (admin roster control) -------------------------------------------

@pytest.mark.asyncio
async def test_remove_team_during_registration(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        await register_team(session, tournament.id, "Alpha", "42")
        await session.commit()

        await remove_team(session, tournament.id, "alpha")  # case-insensitive
        await session.commit()
        assert await list_participants(session, tournament.id) == []

        with pytest.raises(ValueError):  # unknown team
            await remove_team(session, tournament.id, "Ghost")


@pytest.mark.asyncio
async def test_remove_team_rejected_once_started(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        await register_team(session, tournament.id, "Alpha", "1")
        await register_team(session, tournament.id, "Bravo", "2")
        await session.commit()
        await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()

        with pytest.raises(ValueError):
            await remove_team(session, tournament.id, "Alpha")


# ---- slice 2: start / results / rounds / standings -------------------------------

async def _tournament_with_teams(session, count, rounds=3):
    tournament = await create_tournament(session, "g1", "Spring", rounds)
    await session.commit()
    for i in range(count):
        await register_team(session, tournament.id, f"Team{i}", str(i))
    await session.commit()
    return tournament


@pytest.mark.asyncio
async def test_start_tournament_activates_and_pairs_round_one(test_db):
    async with test_db() as session:
        tournament = await _tournament_with_teams(session, 4)
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()

        assert tournament.status == "active"
        assert tournament.current_round == 1
        assert len(matches) == 2
        rounds = (await session.execute(select(TournamentRound))).scalars().all()
        assert len(rounds) == 1 and rounds[0].round_number == 1
        paired = {m.team_a_participant_id for m in matches} | {m.team_b_participant_id for m in matches}
        assert len(paired) == 4


@pytest.mark.asyncio
async def test_start_tournament_odd_count_scores_the_bye(test_db):
    async with test_db() as session:
        tournament = await _tournament_with_teams(session, 5)
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()

        byes = [m for m in matches if m.is_bye]
        assert len(byes) == 1
        bye_match = byes[0]
        assert bye_match.team_b_participant_id is None

        recipient = await session.get(TournamentParticipant, bye_match.team_a_participant_id)
        assert recipient.match_wins == 1
        assert recipient.points == 3
        assert recipient.byes == 1


@pytest.mark.asyncio
async def test_start_tournament_requires_registration_status_and_two_teams(test_db):
    async with test_db() as session:
        tournament = await _tournament_with_teams(session, 1)
        with pytest.raises(ValueError):
            await start_tournament(session, tournament.id, random.Random(7))

        await register_team(session, tournament.id, "Other", "9")
        await session.commit()
        await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        with pytest.raises(ValueError):  # already active
            await start_tournament(session, tournament.id, random.Random(7))


@pytest.mark.asyncio
async def test_set_result_updates_both_participants(test_db):
    async with test_db() as session:
        tournament = await _tournament_with_teams(session, 2)
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()

        match = await set_result(session, matches[0].id, 2, 1)
        await session.commit()

        winner = await session.get(TournamentParticipant, match.team_a_participant_id)
        loser = await session.get(TournamentParticipant, match.team_b_participant_id)
        assert (winner.match_wins, winner.match_losses, winner.points) == (1, 0, 3)
        assert (winner.game_wins, winner.game_losses) == (2, 1)
        assert (loser.match_wins, loser.match_losses, loser.points) == (0, 1, 0)
        assert (loser.game_wins, loser.game_losses) == (1, 2)


@pytest.mark.asyncio
async def test_set_result_draw_gives_one_point_each(test_db):
    async with test_db() as session:
        tournament = await _tournament_with_teams(session, 2)
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()

        match = await set_result(session, matches[0].id, 1, 1)
        await session.commit()

        a = await session.get(TournamentParticipant, match.team_a_participant_id)
        b = await session.get(TournamentParticipant, match.team_b_participant_id)
        assert a.match_draws == 1 and b.match_draws == 1
        assert a.points == 1 and b.points == 1


@pytest.mark.asyncio
async def test_set_result_correction_replaces_not_doubles(test_db):
    async with test_db() as session:
        tournament = await _tournament_with_teams(session, 2)
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()

        await set_result(session, matches[0].id, 2, 0)
        await session.commit()
        match = await set_result(session, matches[0].id, 0, 2)  # admin correction
        await session.commit()

        a = await session.get(TournamentParticipant, match.team_a_participant_id)
        b = await session.get(TournamentParticipant, match.team_b_participant_id)
        assert (a.match_wins, a.match_losses, a.points) == (0, 1, 0)
        assert (b.match_wins, b.match_losses, b.points) == (1, 0, 3)
        assert (a.game_wins, a.game_losses) == (0, 2)
        assert (b.game_wins, b.game_losses) == (2, 0)


@pytest.mark.asyncio
async def test_set_result_rejects_bye_matches(test_db):
    async with test_db() as session:
        tournament = await _tournament_with_teams(session, 3)
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()

        bye_match = next(m for m in matches if m.is_bye)
        with pytest.raises(ValueError):
            await set_result(session, bye_match.id, 2, 0)


@pytest.mark.asyncio
async def test_find_current_match_resolves_by_team_name(test_db):
    async with test_db() as session:
        tournament = await _tournament_with_teams(session, 4)
        await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()

        match = await find_current_match(session, tournament.id, "Team2")
        participants = {match.team_a_participant_id, match.team_b_participant_id}
        team2 = (await session.execute(
            select(TournamentParticipant).where(TournamentParticipant.team_name == "Team2")
        )).scalars().one()
        assert team2.id in participants

        assert await find_current_match(session, tournament.id, "NoSuchTeam") is None


@pytest.mark.asyncio
async def test_advance_round_gated_until_all_results_in(test_db):
    async with test_db() as session:
        tournament = await _tournament_with_teams(session, 4)
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()

        with pytest.raises(ValueError):
            await advance_round(session, tournament.id, random.Random(7))

        await set_result(session, matches[0].id, 2, 0)
        await set_result(session, matches[1].id, 2, 1)
        await session.commit()

        new_round = await advance_round(session, tournament.id, random.Random(7))
        await session.commit()
        assert new_round.round_number == 2
        assert tournament.current_round == 2


@pytest.mark.asyncio
async def test_advance_round_pairs_winners_and_avoids_rematch(test_db):
    async with test_db() as session:
        tournament = await _tournament_with_teams(session, 4)
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        await set_result(session, matches[0].id, 2, 0)
        await set_result(session, matches[1].id, 2, 0)
        await session.commit()
        await advance_round(session, tournament.id, random.Random(7))
        await session.commit()

        round_two = (await session.execute(
            select(TournamentRound).where(TournamentRound.round_number == 2)
        )).scalars().one()
        new_matches = (await session.execute(
            select(TournamentMatch).where(TournamentMatch.round_id == round_two.id)
        )).scalars().all()

        round_one_pairs = {
            frozenset((m.team_a_participant_id, m.team_b_participant_id)) for m in matches
        }
        winners = {matches[0].team_a_participant_id, matches[1].team_a_participant_id}
        for m in new_matches:
            pair = frozenset((m.team_a_participant_id, m.team_b_participant_id))
            assert pair not in round_one_pairs
            # winners (3 pts) face each other, losers face each other
            assert (m.team_a_participant_id in winners) == (m.team_b_participant_id in winners)


@pytest.mark.asyncio
async def test_advance_after_final_round_completes_tournament(test_db):
    async with test_db() as session:
        tournament = await _tournament_with_teams(session, 2, rounds=1)
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        await set_result(session, matches[0].id, 2, 0)
        await session.commit()

        result = await advance_round(session, tournament.id, random.Random(7))
        await session.commit()
        assert result is None
        assert tournament.status == "completed"
        assert await get_active_tournament(session, "g1") is None


@pytest.mark.asyncio
async def test_standings_uses_omw_to_break_points_tie(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()
        # Two 3-point teams with equal game diff. 'zeta' beat a winner, 'alpha'
        # beat a loser. Names are chosen so the name tiebreaker would put alpha
        # FIRST — only OMW% can flip zeta above alpha.
        ids = {}
        for name, pts, w, l, gw, gl in [
            ("zeta", 3, 1, 0, 2, 0), ("alpha", 3, 1, 0, 2, 0),
            ("good", 3, 1, 0, 2, 0), ("bad", 0, 0, 1, 0, 2),
        ]:
            p = TournamentParticipant(
                tournament_id=tournament.id, team_id=len(ids) + 1, team_name=name,
                captain_user_id="1", points=pts, match_wins=w, match_losses=l,
                game_wins=gw, game_losses=gl,
            )
            session.add(p)
            await session.flush()
            ids[name] = p.id
        round_one = TournamentRound(tournament_id=tournament.id, round_number=1)
        session.add(round_one)
        await session.flush()
        session.add(TournamentMatch(round_id=round_one.id,
                                    team_a_participant_id=ids["zeta"],
                                    team_b_participant_id=ids["good"],
                                    team_a_wins=2, team_b_wins=0))
        session.add(TournamentMatch(round_id=round_one.id,
                                    team_a_participant_id=ids["alpha"],
                                    team_b_participant_id=ids["bad"],
                                    team_a_wins=2, team_b_wins=0))
        await session.commit()

        standings = await get_standings_data(session, tournament.id)
        order = [p.team_name for p in standings]
        assert order.index("zeta") < order.index("alpha"), (
            f"OMW% should rank 'zeta' above 'alpha'; got {order}"
        )


@pytest.mark.asyncio
async def test_standings_sorted_by_points_then_game_diff(test_db):
    async with test_db() as session:
        tournament = await _tournament_with_teams(session, 4)
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        await set_result(session, matches[0].id, 2, 0)  # winner: +2 game diff
        await set_result(session, matches[1].id, 2, 1)  # winner: +1 game diff
        await session.commit()

        standings = await get_standings_data(session, tournament.id)
        assert [p.points for p in standings] == [3, 3, 0, 0]
        assert standings[0].id == matches[0].team_a_participant_id  # better game diff first
        assert standings[1].id == matches[1].team_a_participant_id


# ---- slice 6: round-robin format + finish ----------------------------------------

async def _round_robin_with_teams(session, count):
    tournament = await create_tournament(session, "g1", "RR", 0, format="round_robin")
    await session.commit()
    for i in range(count):
        await register_team(session, tournament.id, f"Team{i}", str(i))
    await session.commit()
    return tournament


@pytest.mark.asyncio
async def test_start_round_robin_creates_full_schedule(test_db):
    async with test_db() as session:
        tournament = await _round_robin_with_teams(session, 4)
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()

        assert tournament.status == "active"
        assert tournament.total_rounds == 3  # n-1 rounds for 4 teams
        rounds = (await session.execute(select(TournamentRound))).scalars().all()
        assert len(rounds) == 3
        # 6 matches total (C(4,2)), none are byes
        assert len(matches) == 6
        assert all(not m.is_bye for m in matches)
        # every team pair appears exactly once
        pairs = {frozenset((m.team_a_participant_id, m.team_b_participant_id)) for m in matches}
        assert len(pairs) == 6


@pytest.mark.asyncio
async def test_next_round_rejected_for_round_robin(test_db):
    async with test_db() as session:
        tournament = await _round_robin_with_teams(session, 4)
        await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        with pytest.raises(ValueError):
            await advance_round(session, tournament.id, random.Random(7))


@pytest.mark.asyncio
async def test_finish_tournament_completes_and_returns_champion(test_db):
    async with test_db() as session:
        tournament = await _round_robin_with_teams(session, 4)
        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        # Give one team a clean sweep so the champion is unambiguous.
        winner_id = matches[0].team_a_participant_id
        for m in matches:
            if m.team_a_participant_id == winner_id:
                await set_result(session, m.id, 2, 0)
            elif m.team_b_participant_id == winner_id:
                await set_result(session, m.id, 0, 2)
        await session.commit()

        champion = await finish_tournament(session, tournament.id)
        await session.commit()

        assert tournament.status == "completed"
        assert champion.id == winner_id
        assert await get_active_tournament(session, "g1") is None


@pytest.mark.asyncio
async def test_finish_rejects_non_active(test_db):
    async with test_db() as session:
        tournament = await _round_robin_with_teams(session, 4)  # still registration
        with pytest.raises(ValueError):
            await finish_tournament(session, tournament.id)


# ---- slice 7: manual schedule ----------------------------------------------------

async def _manual_with_teams(session, count):
    tournament = await create_tournament(session, "g1", "Manual", 0, format="manual")
    await session.commit()
    for i in range(count):
        await register_team(session, tournament.id, f"Team{i}", str(i))
    await session.commit()
    return tournament


@pytest.mark.asyncio
async def test_add_match_creates_authored_match(test_db):
    async with test_db() as session:
        tournament = await _manual_with_teams(session, 4)
        match = await add_match(session, tournament.id, "Team0", "Team1")
        await session.commit()

        p0 = (await session.execute(select(TournamentParticipant).where(
            TournamentParticipant.team_name == "Team0"))).scalars().one()
        p1 = (await session.execute(select(TournamentParticipant).where(
            TournamentParticipant.team_name == "Team1"))).scalars().one()
        assert {match.team_a_participant_id, match.team_b_participant_id} == {p0.id, p1.id}
        assert match.is_bye is False


@pytest.mark.asyncio
async def test_add_match_resolves_team_names_case_insensitively(test_db):
    async with test_db() as session:
        tournament = await _manual_with_teams(session, 4)
        match = await add_match(session, tournament.id, "  team0 ", "TEAM1")
        await session.commit()
        assert match.id is not None


@pytest.mark.asyncio
async def test_add_match_rejects_unknown_team(test_db):
    async with test_db() as session:
        tournament = await _manual_with_teams(session, 2)
        with pytest.raises(ValueError):
            await add_match(session, tournament.id, "Team0", "Ghost")


@pytest.mark.asyncio
async def test_add_match_rejects_self_pairing(test_db):
    async with test_db() as session:
        tournament = await _manual_with_teams(session, 2)
        with pytest.raises(ValueError):
            await add_match(session, tournament.id, "Team0", "Team0")


@pytest.mark.asyncio
async def test_add_match_rejected_for_non_manual_format(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "RR", 0, format="round_robin")
        await session.commit()
        await register_team(session, tournament.id, "A", "1")
        await register_team(session, tournament.id, "B", "2")
        await session.commit()
        with pytest.raises(ValueError):
            await add_match(session, tournament.id, "A", "B")


@pytest.mark.asyncio
async def test_add_match_rejected_after_start(test_db):
    async with test_db() as session:
        tournament = await _manual_with_teams(session, 2)
        await add_match(session, tournament.id, "Team0", "Team1")
        await session.commit()
        await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        with pytest.raises(ValueError):
            await add_match(session, tournament.id, "Team0", "Team1")


@pytest.mark.asyncio
async def test_add_match_packs_into_capped_rounds(test_db):
    async with test_db() as session:
        tournament = await _manual_with_teams(session, 6)
        # 12 matches with a per-round cap of 10 -> 2 rounds
        names = [f"Team{i}" for i in range(6)]
        added = 0
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                await add_match(session, tournament.id, names[i], names[j])
                added += 1
        await session.commit()
        assert added == 15  # C(6,2)
        rounds = (await session.execute(select(TournamentRound))).scalars().all()
        # 15 matches, cap 10 -> 2 rounds; no round exceeds the cap
        assert len(rounds) == 2
        for r in rounds:
            cnt = len((await session.execute(select(TournamentMatch).where(
                TournamentMatch.round_id == r.id))).scalars().all())
            assert cnt <= 10


@pytest.mark.asyncio
async def test_start_manual_opens_authored_schedule(test_db):
    async with test_db() as session:
        tournament = await _manual_with_teams(session, 4)
        await add_match(session, tournament.id, "Team0", "Team1")
        await add_match(session, tournament.id, "Team2", "Team3")
        await session.commit()

        matches = await start_tournament(session, tournament.id, random.Random(7))
        await session.commit()
        assert tournament.status == "active"
        assert tournament.total_rounds == 1  # both fit in one capped round
        assert len(matches) == 2


@pytest.mark.asyncio
async def test_start_manual_requires_authored_matches(test_db):
    async with test_db() as session:
        tournament = await _manual_with_teams(session, 4)  # no matches added
        with pytest.raises(ValueError):
            await start_tournament(session, tournament.id, random.Random(7))


# ---- list_participants ----------------------------------------------------------

@pytest.mark.asyncio
async def test_list_participants_in_registration_order(test_db):
    async with test_db() as session:
        tournament = await create_tournament(session, "g1", "Spring", 3)
        await session.commit()

        for name, captain in (("Bravo", "1"), ("Alpha", "2"), ("Charlie", "3")):
            await register_team(session, tournament.id, name, captain)
            await session.commit()

        participants = await list_participants(session, tournament.id)
        assert [p.team_name for p in participants] == ["Bravo", "Alpha", "Charlie"]


# ---- team rosters ---------------------------------------------------------------

async def _tournament_with_team(session, team="Alpha", captain="42"):
    """A registration-open tournament with one registered team."""
    tournament = await create_tournament(session, "g1", "Spring", 3)
    await session.commit()
    participant, _ = await register_team(session, tournament.id, team, captain)
    await session.commit()
    return tournament, participant


@pytest.mark.asyncio
async def test_add_teammate_puts_the_player_on_the_roster(test_db):
    async with test_db() as session:
        tournament, participant = await _tournament_with_team(session)

        member, created = await add_teammate(session, participant, "7", "Bob")
        await session.commit()

        assert created is True
        assert member.user_id == "7"
        assert member.display_name == "Bob"
        rosters = await get_rosters(session, tournament.id)
        assert [m.user_id for m in rosters[participant.id]] == ["7"]


@pytest.mark.asyncio
async def test_add_teammate_is_idempotent(test_db):
    """Re-adding someone already on the roster is a no-op, not a duplicate row."""
    async with test_db() as session:
        tournament, participant = await _tournament_with_team(session)

        await add_teammate(session, participant, "7", "Bob")
        await session.commit()
        member, created = await add_teammate(session, participant, "7", "Bob Renamed")
        await session.commit()

        assert created is False
        rosters = await get_rosters(session, tournament.id)
        assert len(rosters[participant.id]) == 1
        # The stored snapshot is not overwritten by a later add.
        assert member.display_name == "Bob"


@pytest.mark.asyncio
async def test_add_teammate_rejects_the_captain(test_db):
    """The captain is already on the team via captain_user_id."""
    async with test_db() as session:
        _, participant = await _tournament_with_team(session, captain="42")

        with pytest.raises(ValueError, match="captain"):
            await add_teammate(session, participant, "42", "Cap")


@pytest.mark.asyncio
async def test_add_teammate_rejects_a_player_on_another_team(test_db):
    """A player may only appear on one roster per tournament."""
    async with test_db() as session:
        tournament, alpha = await _tournament_with_team(session, "Alpha", "42")
        bravo, _ = await register_team(session, tournament.id, "Bravo", "99")
        await session.commit()

        await add_teammate(session, alpha, "7", "Bob")
        await session.commit()

        with pytest.raises(ValueError, match="Alpha"):
            await add_teammate(session, bravo, "7", "Bob")


@pytest.mark.asyncio
async def test_add_teammate_allows_the_same_player_in_a_different_tournament(test_db):
    """The one-team rule is scoped per tournament, not globally."""
    async with test_db() as session:
        _, alpha = await _tournament_with_team(session, "Alpha", "42")
        await add_teammate(session, alpha, "7", "Bob")
        await session.commit()

        other = await create_tournament(session, "g2", "Autumn", 3)
        await session.commit()
        echo, _ = await register_team(session, other.id, "Echo", "55")
        await session.commit()

        _, created = await add_teammate(session, echo, "7", "Bob")
        await session.commit()
        assert created is True


@pytest.mark.asyncio
async def test_add_teammate_rejects_a_completed_tournament(test_db):
    async with test_db() as session:
        tournament, participant = await _tournament_with_team(session)
        tournament.status = "completed"
        await session.commit()

        with pytest.raises(ValueError, match="completed"):
            await add_teammate(session, participant, "7", "Bob")


@pytest.mark.asyncio
async def test_add_teammate_allowed_after_the_tournament_starts(test_db):
    """Rosters stay editable mid-event; only a finished tournament is frozen."""
    async with test_db() as session:
        tournament, participant = await _tournament_with_team(session)
        tournament.status = "active"
        await session.commit()

        _, created = await add_teammate(session, participant, "7", "Bob")
        await session.commit()
        assert created is True


@pytest.mark.asyncio
async def test_remove_teammate_takes_the_player_off_the_roster(test_db):
    async with test_db() as session:
        tournament, participant = await _tournament_with_team(session)
        await add_teammate(session, participant, "7", "Bob")
        await session.commit()

        removed = await remove_teammate(session, participant, "7")
        await session.commit()

        assert removed is True
        rosters = await get_rosters(session, tournament.id)
        assert rosters.get(participant.id, []) == []


@pytest.mark.asyncio
async def test_remove_teammate_reports_when_the_player_was_not_on_the_roster(test_db):
    async with test_db() as session:
        _, participant = await _tournament_with_team(session)

        assert await remove_teammate(session, participant, "7") is False


@pytest.mark.asyncio
async def test_find_participant_for_captain(test_db):
    async with test_db() as session:
        tournament, participant = await _tournament_with_team(session, "Alpha", "42")

        found = await find_participant_for_captain(session, tournament.id, "42")
        assert found is not None and found.id == participant.id
        assert await find_participant_for_captain(session, tournament.id, "99") is None


@pytest.mark.asyncio
async def test_get_rosters_groups_by_participant_and_stays_in_its_tournament(test_db):
    async with test_db() as session:
        tournament, alpha = await _tournament_with_team(session, "Alpha", "42")
        bravo, _ = await register_team(session, tournament.id, "Bravo", "99")
        await session.commit()
        other = await create_tournament(session, "g2", "Autumn", 3)
        await session.commit()
        echo, _ = await register_team(session, other.id, "Echo", "55")
        await session.commit()

        await add_teammate(session, alpha, "7", "Bob")
        await add_teammate(session, alpha, "8", "Cara")
        await add_teammate(session, bravo, "9", "Dan")
        await add_teammate(session, echo, "10", "Eve")
        await session.commit()

        rosters = await get_rosters(session, tournament.id)
        assert sorted(m.user_id for m in rosters[alpha.id]) == ["7", "8"]
        assert [m.user_id for m in rosters[bravo.id]] == ["9"]
        assert echo.id not in rosters


@pytest.mark.asyncio
async def test_remove_team_also_deletes_its_roster(test_db):
    """Dropping a team must not leave its members orphaned behind it."""
    async with test_db() as session:
        tournament, participant = await _tournament_with_team(session, "Alpha", "42")
        await add_teammate(session, participant, "7", "Bob")
        await session.commit()

        await remove_team(session, tournament.id, "Alpha")
        await session.commit()

        left = (await session.execute(select(TournamentTeamMember))).scalars().all()
        assert left == []


@pytest.mark.asyncio
async def test_add_teammate_rejects_another_teams_captain(test_db):
    """A captain is on their own team even with no roster row to say so."""
    async with test_db() as session:
        tournament, alpha = await _tournament_with_team(session, "Alpha", "42")
        await register_team(session, tournament.id, "Bravo", "99")
        await session.commit()

        with pytest.raises(ValueError, match="Bravo"):
            await add_teammate(session, alpha, "99", "Cap Two")


@pytest.mark.asyncio
async def test_remove_teammate_rejects_a_completed_tournament(test_db):
    """Rosters of a finished tournament are a record, so removal is frozen too."""
    async with test_db() as session:
        tournament, participant = await _tournament_with_team(session)
        await add_teammate(session, participant, "7", "Bob")
        await session.commit()
        tournament.status = "completed"
        await session.commit()

        with pytest.raises(ValueError, match="completed"):
            await remove_teammate(session, participant, "7")
