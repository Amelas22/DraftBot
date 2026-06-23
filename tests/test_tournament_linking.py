"""Tests for services/tournament_linking.py."""
import os
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
from models.draft_session import DraftSession
from models.tournament import (
    Tournament, TournamentMatch, TournamentParticipant, TournamentRound,
)
from services.tournament_linking import resolve_candidate_matches


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


async def _make_tournament(session, n_parts=2):
    t = Tournament(guild_id="g1", name="T", total_rounds=1, current_round=1,
                   status="active", format="manual")
    session.add(t)
    await session.flush()
    r = TournamentRound(tournament_id=t.id, round_number=1)
    session.add(r)
    await session.flush()
    names = ["Latecomers", "Strixhaven Dropouts", "Rakdos Intolerant", "European Juggernauts"]
    parts = []
    for i in range(n_parts):
        p = TournamentParticipant(tournament_id=t.id, team_id=i + 1,
                                  team_name=names[i], captain_user_id=str(i))
        session.add(p)
        parts.append(p)
    await session.flush()
    return t, r, parts


@pytest.mark.asyncio
async def test_exact_name_match_one_candidate(test_db):
    async with test_db() as s:
        t, r, parts = await _make_tournament(s)
        m = TournamentMatch(round_id=r.id, team_a_participant_id=parts[0].id,
                            team_b_participant_id=parts[1].id)
        s.add(m); await s.flush()
        out = await resolve_candidate_matches(s, t, "Latecomers", "Strixhaven Dropouts")
    assert len(out) == 1
    assert out[0].reversed is False
    assert out[0].round_number == 1
    assert (out[0].a_name, out[0].b_name) == ("Latecomers", "Strixhaven Dropouts")


@pytest.mark.asyncio
async def test_reversed_orientation(test_db):
    async with test_db() as s:
        t, r, parts = await _make_tournament(s)
        m = TournamentMatch(round_id=r.id, team_a_participant_id=parts[0].id,
                            team_b_participant_id=parts[1].id)
        s.add(m); await s.flush()
        out = await resolve_candidate_matches(s, t, "Strixhaven Dropouts", "Latecomers")
    assert len(out) == 1
    assert out[0].reversed is True


@pytest.mark.asyncio
async def test_substring_abbreviation_matches(test_db):
    async with test_db() as s:
        t, r, parts = await _make_tournament(s)
        m = TournamentMatch(round_id=r.id, team_a_participant_id=parts[0].id,
                            team_b_participant_id=parts[1].id)
        s.add(m); await s.flush()
        out = await resolve_candidate_matches(s, t, "latecomers", "strixhaven")
    assert len(out) == 1


@pytest.mark.asyncio
async def test_below_threshold_no_candidate(test_db):
    async with test_db() as s:
        t, r, parts = await _make_tournament(s)
        m = TournamentMatch(round_id=r.id, team_a_participant_id=parts[0].id,
                            team_b_participant_id=parts[1].id)
        s.add(m); await s.flush()
        out = await resolve_candidate_matches(s, t, "Zzzzzz", "Qqqqqq")
    assert out == []


@pytest.mark.asyncio
async def test_played_match_excluded(test_db):
    async with test_db() as s:
        t, r, parts = await _make_tournament(s)
        m = TournamentMatch(round_id=r.id, team_a_participant_id=parts[0].id,
                            team_b_participant_id=parts[1].id, team_a_wins=5, team_b_wins=4)
        s.add(m); await s.flush()
        out = await resolve_candidate_matches(s, t, "Latecomers", "Strixhaven Dropouts")
    assert out == []


@pytest.mark.asyncio
async def test_already_linked_match_excluded(test_db):
    async with test_db() as s:
        t, r, parts = await _make_tournament(s)
        m = TournamentMatch(round_id=r.id, team_a_participant_id=parts[0].id,
                            team_b_participant_id=parts[1].id)
        s.add(m); await s.flush()
        s.add(DraftSession(session_id="other", guild_id="g1", session_type="premade",
                           tournament_match_id=m.id))
        await s.flush()
        out = await resolve_candidate_matches(s, t, "Latecomers", "Strixhaven Dropouts")
    assert out == []


@pytest.mark.asyncio
async def test_bye_excluded(test_db):
    async with test_db() as s:
        t, r, parts = await _make_tournament(s)
        m = TournamentMatch(round_id=r.id, team_a_participant_id=parts[0].id,
                            team_b_participant_id=None, is_bye=True)
        s.add(m); await s.flush()
        out = await resolve_candidate_matches(s, t, "Latecomers", "Strixhaven Dropouts")
    assert out == []


@pytest.mark.asyncio
async def test_multiple_candidates_sorted_by_confidence(test_db):
    # Two genuinely-matching unplayed matches with DISTINCT confidences:
    # m1 pairs the exact names (conf 1.0); m2's opponent is a near-miss spelling
    # ("...Dropoutz", difflib ~0.95, no substring boost), so it ranks lower.
    async with test_db() as s:
        t = Tournament(guild_id="g1", name="T", total_rounds=1, current_round=1,
                       status="active", format="manual")
        s.add(t); await s.flush()
        r = TournamentRound(tournament_id=t.id, round_number=1); s.add(r); await s.flush()
        p0 = TournamentParticipant(tournament_id=t.id, team_id=1, team_name="Latecomers",
                                   captain_user_id="0")
        p1 = TournamentParticipant(tournament_id=t.id, team_id=2, team_name="Strixhaven Dropouts",
                                   captain_user_id="1")
        p2 = TournamentParticipant(tournament_id=t.id, team_id=3, team_name="Strixhaven Dropoutz",
                                   captain_user_id="2")
        s.add_all([p0, p1, p2]); await s.flush()
        m1 = TournamentMatch(round_id=r.id, team_a_participant_id=p0.id, team_b_participant_id=p1.id)
        m2 = TournamentMatch(round_id=r.id, team_a_participant_id=p0.id, team_b_participant_id=p2.id)
        s.add_all([m1, m2]); await s.flush()
        out = await resolve_candidate_matches(s, t, "Latecomers", "Strixhaven Dropouts")
        ids = [c.match_id for c in out]
        m1id, m2id = m1.id, m2.id
    assert len(out) == 2
    assert set(ids) == {m1id, m2id}
    assert out[0].match_id == m1id          # exact match ranks first
    assert out[0].confidence > out[1].confidence


@pytest.mark.asyncio
async def test_substring_boost_admits_low_raw_ratio_name(test_db):
    # "rakdos" has a low raw difflib ratio against the long team name, but is a
    # substring, so the forced-0.90 boost (and the boost alone) admits it.
    async with test_db() as s:
        t, r, parts = await _make_tournament(s)
        # Rename participant A to a long name that contains "rakdos".
        parts[0].team_name = "Mono-Red Rakdos Midrange Intolerant Crew"
        await s.flush()
        m = TournamentMatch(round_id=r.id, team_a_participant_id=parts[0].id,
                            team_b_participant_id=parts[1].id)
        s.add(m); await s.flush()
        out = await resolve_candidate_matches(s, t, "rakdos", "Strixhaven Dropouts")
    assert len(out) == 1  # admitted only because "rakdos" is contained (boost -> 0.9)
