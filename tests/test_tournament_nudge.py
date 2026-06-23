"""Tests for tournament_nudge.py view construction."""
import pytest
from services.tournament_linking import CandidateLink
from tournament_nudge import (
    TournamentLinkButtonView,
    TournamentLinkSelectView,
    build_nudge_view,
)


def _cand(match_id, a="Latecomers", b="Strixhaven Dropouts", rnd=2, conf=0.9):
    return CandidateLink(match_id=match_id, reversed=False, confidence=conf,
                         a_name=a, b_name=b, round_number=rnd)


@pytest.mark.asyncio
async def test_no_candidates_returns_none():
    assert build_nudge_view("s1", []) is None


@pytest.mark.asyncio
async def test_single_candidate_builds_button_view():
    content, view = build_nudge_view("s1", [_cand(10)])
    assert isinstance(view, TournamentLinkButtonView)
    assert "Latecomers" in content and "Strixhaven Dropouts" in content
    button = view.children[0]
    assert button.custom_id == "tourney_link:s1:10"


@pytest.mark.asyncio
async def test_multiple_candidates_build_select_view():
    content, view = build_nudge_view("s1", [_cand(10), _cand(11, a="Rakdos Intolerant",
                                                             b="European Juggernauts", rnd=3)])
    assert isinstance(view, TournamentLinkSelectView)
    select = view.children[0]
    assert select.custom_id == "tourney_link_select:s1"
    values = {opt.value for opt in select.options}
    assert values == {"10", "11"}


import os
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import database.db_session as db_session_module
from database.models_base import Base
from models.draft_session import DraftSession
from models.tournament import (
    Tournament, TournamentMatch, TournamentParticipant, TournamentRound,
)
from sqlalchemy import select


@pytest_asyncio.fixture
async def patched_db(monkeypatch):
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    # Point the module-level db_session() used by perform_link at our temp DB.
    monkeypatch.setattr(db_session_module, "AsyncSessionLocal", factory, raising=False)
    yield factory
    await engine.dispose()
    os.unlink(temp_db.name)


@pytest.mark.asyncio
async def test_perform_link_links_draft(patched_db):
    from tournament_nudge import perform_link
    async with patched_db() as s:
        t = Tournament(guild_id="g1", name="T", total_rounds=1, current_round=1,
                       status="active", format="manual")
        s.add(t); await s.flush()
        r = TournamentRound(tournament_id=t.id, round_number=1); s.add(r); await s.flush()
        pa = TournamentParticipant(tournament_id=t.id, team_id=1, team_name="Latecomers",
                                   captain_user_id="1")
        pb = TournamentParticipant(tournament_id=t.id, team_id=2, team_name="Strixhaven Dropouts",
                                   captain_user_id="2")
        s.add_all([pa, pb]); await s.flush()
        m = TournamentMatch(round_id=r.id, team_a_participant_id=pa.id, team_b_participant_id=pb.id)
        s.add(m); await s.flush()
        s.add(DraftSession(session_id="d1", guild_id="g1", session_type="premade",
                           team_a_name="Latecomers", team_b_name="Strixhaven Dropouts"))
        await s.commit()
        match_id = m.id

    from tournament_nudge import perform_link
    outcome = await perform_link("d1", match_id, "u1")
    assert outcome.status == "linked"

    async with patched_db() as s:
        d = (await s.execute(select(DraftSession).where(DraftSession.session_id == "d1"))).scalar_one()
    assert d.tournament_match_id == match_id


async def _seed_match(factory, draft_a, draft_b):
    """Create tournament t/round/p0(Latecomers)/p1(Strixhaven) + match + draft d1."""
    async with factory() as s:
        t = Tournament(guild_id="g1", name="T", total_rounds=1, current_round=1,
                       status="active", format="manual")
        s.add(t); await s.flush()
        r = TournamentRound(tournament_id=t.id, round_number=1); s.add(r); await s.flush()
        pa = TournamentParticipant(tournament_id=t.id, team_id=1, team_name="Latecomers",
                                   captain_user_id="1")
        pb = TournamentParticipant(tournament_id=t.id, team_id=2, team_name="Strixhaven Dropouts",
                                   captain_user_id="2")
        s.add_all([pa, pb]); await s.flush()
        m = TournamentMatch(round_id=r.id, team_a_participant_id=pa.id, team_b_participant_id=pb.id)
        s.add(m); await s.flush()
        s.add(DraftSession(session_id="d1", guild_id="g1", session_type="premade",
                           team_a_name=draft_a, team_b_name=draft_b))
        await s.commit()
        return m.id, pa.id, pb.id


@pytest.mark.asyncio
async def test_perform_link_reversed_swaps_through_wrapper(patched_db):
    from tournament_nudge import perform_link
    # Draft team A = "Strixhaven Dropouts" = match participant B -> reversed.
    match_id, pa_id, pb_id = await _seed_match(patched_db, "Strixhaven Dropouts", "Latecomers")
    outcome = await perform_link("d1", match_id, "u1")
    assert outcome.status == "linked"
    async with patched_db() as s:
        m = (await s.execute(select(TournamentMatch).where(TournamentMatch.id == match_id))).scalar_one()
        d = (await s.execute(select(DraftSession).where(DraftSession.session_id == "d1"))).scalar_one()
    assert (m.team_a_participant_id, m.team_b_participant_id) == (pb_id, pa_id)  # swapped
    assert d.tournament_match_id == match_id


class _FakeMsg:
    def __init__(self):
        self.edits = []

    async def edit(self, content=None, view="__unset__"):
        self.edits.append((content, view))


@pytest.mark.asyncio
async def test_apply_confirmation_success_edits_public_with_attribution(patched_db):
    from tournament_nudge import apply_confirmation
    match_id, _, _ = await _seed_match(patched_db, "Latecomers", "Strixhaven Dropouts")
    msg = _FakeMsg()
    note = await apply_confirmation("d1", match_id, "u1", "<@u1>", msg)
    assert "Linked" in note
    # public control dropped (view=None) and the acting user is shown
    assert len(msg.edits) == 1
    content, view = msg.edits[0]
    assert view is None
    assert "<@u1>" in content
    assert "Latecomers" in content and "Strixhaven Dropouts" in content


@pytest.mark.asyncio
async def test_apply_confirmation_failure_drops_control(patched_db):
    from tournament_nudge import apply_confirmation
    match_id, _, _ = await _seed_match(patched_db, "Latecomers", "Strixhaven Dropouts")
    # Pre-link the match to a different draft so this attempt fails as match_taken.
    async with patched_db() as s:
        s.add(DraftSession(session_id="other", guild_id="g1", session_type="premade",
                           tournament_match_id=match_id))
        await s.commit()
    msg = _FakeMsg()
    note = await apply_confirmation("d1", match_id, "u1", "<@u1>", msg)
    assert note.startswith("❌")
    assert msg.edits == [(note, None)] or (len(msg.edits) == 1 and msg.edits[0][1] is None)


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, view=None):
        self.sent.append((content, view))
        return object()


@pytest.mark.asyncio
async def test_post_premade_nudge_posts_when_candidate(patched_db):
    from tournament_nudge import post_premade_nudge, TournamentLinkButtonView
    async with patched_db() as s:
        t = Tournament(guild_id="g1", name="T", total_rounds=1, current_round=1,
                       status="active", format="manual")
        s.add(t); await s.flush()
        r = TournamentRound(tournament_id=t.id, round_number=1); s.add(r); await s.flush()
        pa = TournamentParticipant(tournament_id=t.id, team_id=1, team_name="Latecomers",
                                   captain_user_id="1")
        pb = TournamentParticipant(tournament_id=t.id, team_id=2, team_name="Strixhaven Dropouts",
                                   captain_user_id="2")
        s.add_all([pa, pb]); await s.flush()
        s.add(TournamentMatch(round_id=r.id, team_a_participant_id=pa.id, team_b_participant_id=pb.id))
        await s.commit()

    ch = _FakeChannel()
    await post_premade_nudge(ch, "g1", "d1", "Latecomers", "Strixhaven Dropouts")
    assert len(ch.sent) == 1
    assert isinstance(ch.sent[0][1], TournamentLinkButtonView)


@pytest.mark.asyncio
async def test_post_premade_nudge_silent_without_tournament(patched_db):
    from tournament_nudge import post_premade_nudge
    ch = _FakeChannel()
    await post_premade_nudge(ch, "g1", "d1", "Latecomers", "Strixhaven Dropouts")
    assert ch.sent == []


@pytest.mark.asyncio
async def test_post_premade_nudge_silent_when_draft_already_linked(patched_db):
    # A ▶ Play-button launch creates a premade draft with tournament_match_id set;
    # the hook must NOT nudge it (spec guard #2).
    from tournament_nudge import post_premade_nudge
    async with patched_db() as s:
        t = Tournament(guild_id="g1", name="T", total_rounds=1, current_round=1,
                       status="active", format="manual")
        s.add(t); await s.flush()
        r = TournamentRound(tournament_id=t.id, round_number=1); s.add(r); await s.flush()
        pa = TournamentParticipant(tournament_id=t.id, team_id=1, team_name="Latecomers",
                                   captain_user_id="1")
        pb = TournamentParticipant(tournament_id=t.id, team_id=2, team_name="Strixhaven Dropouts",
                                   captain_user_id="2")
        s.add_all([pa, pb]); await s.flush()
        m = TournamentMatch(round_id=r.id, team_a_participant_id=pa.id, team_b_participant_id=pb.id)
        s.add(m); await s.flush()
        s.add(DraftSession(session_id="d1", guild_id="g1", session_type="premade",
                           team_a_name="Latecomers", team_b_name="Strixhaven Dropouts",
                           tournament_match_id=m.id))
        await s.commit()
    ch = _FakeChannel()
    await post_premade_nudge(ch, "g1", "d1", "Latecomers", "Strixhaven Dropouts")
    assert ch.sent == []
