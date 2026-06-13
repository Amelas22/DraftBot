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
)
from services.tournament_service import (
    advance_round,
    create_tournament,
    find_current_match,
    get_active_tournament,
    get_standings_data,
    list_participants,
    register_team,
    remove_team,
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
