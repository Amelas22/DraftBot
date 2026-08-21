"""Shared pytest fixtures and seeding helpers.

test_db: a throwaway file-backed SQLite database with the full schema,
wired into the app's AsyncSessionLocal for the duration of one test and
unwired afterward. Many older test files still carry a local copy of this
fixture (which shadows this one, harmlessly); new test files should use
this one.

seed_session: the one seeder for DraftSession + MatchResult fixtures --
import it (``from conftest import seed_session``) instead of hand-writing
another per-file variant.

seed_settlement: the one seeder for a paired settlement DebtLedger write --
import it (``from conftest import seed_settlement``) instead of hand-writing
another per-file variant.

match_control_db: a DIFFERENT throwaway SQLite database from test_db above --
this one yields the raw sessionmaker factory instead of rebinding the app's
global AsyncSessionLocal. Use it for code that takes its own session/engine
rather than going through AsyncSessionLocal (e.g. match_control_view.py,
which opens sessions via database.db_session.db_session). Open sessions with
``async with match_control_db() as session: ...``, or patch a module's own
db_session to route through it -- see test_match_control_flow.py's
patched_db fixture. Use test_db instead for anything that goes through the
app-wide AsyncSessionLocal.

seed_tournament_match: the one seeder for a started 2-team tournament's only
match (Alpha vs Bravo) -- import it (``from conftest import
seed_tournament_match``) instead of hand-writing another per-file variant.
Takes the session to seed into (e.g. one opened via match_control_db).
"""
import os
import random
import tempfile
from datetime import datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models_base import Base
from database.db_session import AsyncSessionLocal
from models.debt_ledger import DebtLedger
from models.draft_session import DraftSession
from models.match import MatchResult
from models.tournament import TournamentMatch
from services.tournament_service import create_tournament, register_team, start_tournament


@pytest_asyncio.fixture
async def test_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    previous_bind = AsyncSessionLocal.kw.get("bind")
    AsyncSessionLocal.configure(bind=engine)
    yield engine
    # Restore the prior binding BEFORE disposing this engine: the factory
    # is process-wide, and leaving it bound to a disposed engine would make
    # any later test that doesn't request this fixture fail on session use
    # (order-dependent breakage).
    AsyncSessionLocal.configure(bind=previous_bind)
    await engine.dispose()
    os.unlink(tmp.name)


@pytest_asyncio.fixture
async def match_control_db():
    """A throwaway file-backed SQLite database, as a raw sessionmaker factory.

    See the module docstring for how this differs from test_db above -- use
    that one instead unless the code under test opens its own sessions
    rather than going through the app-wide AsyncSessionLocal.
    """
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp_db.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()
    os.unlink(temp_db.name)


async def seed_tournament_match(session, thread_id=None):
    """A started 2-team tournament's (Alpha vs Bravo) only match.

    thread_id, if given, is written directly onto the match row -- the shape
    match_facts / control-message tests need without actually creating a
    Discord thread via create_match_room.
    """
    tournament = await create_tournament(session, "g1", "Spring", 3)
    await session.commit()
    await register_team(session, tournament.id, "Alpha", "1")
    await register_team(session, tournament.id, "Bravo", "2")
    await session.commit()
    matches = await start_tournament(session, tournament.id, random.Random(7))
    await session.commit()
    match = await session.get(TournamentMatch, matches[0].id)
    if thread_id:
        match.thread_id = thread_id
    await session.commit()
    return match


async def seed_session(session_id="s1", guild="g", stype="staked",
                       stage="completed", victory=None, teams=None,
                       matches=(), start=None, sign_ups=None,
                       cube="TestCube"):
    """Seed one DraftSession plus its MatchResults.

    teams: (team_a_list, team_b_list) or None (legacy-style, no team JSON).
    matches: iterable of (player1, player2, winner, submitted_at_or_None).
    """
    when = start or datetime(2026, 1, 1)
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(
            session_id=session_id, guild_id=guild, session_type=stype,
            session_stage=stage,
            victory_message_id_results_channel=victory,
            team_a=list(teams[0]) if teams else None,
            team_b=list(teams[1]) if teams else None,
            draft_start_time=when, teams_start_time=when,
            sign_ups=sign_ups, cube=cube))
        for i, (p1, p2, w, ts) in enumerate(matches):
            s.add(MatchResult(session_id=session_id, match_number=i + 1,
                              player1_id=p1, player2_id=p2, winner_id=w,
                              result_submitted_at=ts))
        await s.commit()


async def seed_settlement(guild, payer, payee, amount, method, source_id):
    """Insert a pair of settlement DebtLedger rows directly, with an explicit
    settlement_method -- including None, to simulate a row some other path
    forgot to classify.

    Mirrors the amount convention of both real writers: the payer's entry is
    positive (it reduces what they owe), the payee's is negative (it reduces
    what they're owed).
    """
    async with AsyncSessionLocal() as s:
        s.add(DebtLedger(
            guild_id=guild, player_id=payer, counterparty_id=payee,
            amount=amount, source_type='settlement', source_id=source_id,
            settlement_method=method,
        ))
        s.add(DebtLedger(
            guild_id=guild, player_id=payee, counterparty_id=payer,
            amount=-amount, source_type='settlement', source_id=source_id,
            settlement_method=method,
        ))
        await s.commit()


# --- create_stats_embed fixtures ------------------------------------------
# The /stats embed builder takes three fully-populated timeframe dicts; these
# give tests one shape to override rather than a per-file copy.

def stats_dict(**overrides):
    """A complete timeframe dict for player_stats.create_stats_embed."""
    base = {
        "display_name": "P", "drafts_played": 12, "matches_won": 5, "matches_played": 9,
        "match_win_percentage": 55.0, "trophies_won": 1,
        "team_drafts_played": 4, "team_drafts_won": 2, "team_drafts_tied": 0,
        "team_draft_win_percentage": 50.0,
        "current_win_streak": 0, "longest_win_streak": 3,
        "current_perfect_streak": 0, "longest_perfect_streak": 1,
        "cube_stats": {},
    }
    base.update(overrides)
    return base


class StubUser:
    """Stands in for the discord.User create_stats_embed reads a name off."""
    display_name = "P"


def embed_field(embed, name):
    """The named field of an embed, or None when it wasn't rendered."""
    return next((f for f in embed.fields if f.name == name), None)


def make_manager(**kwargs):
    """A DraftSetupManager with its socket mocked out.

    Four test files had grown their own copy of this. The constructor's signature is
    the thing that keeps changing (packs_per_player, cards_per_pack, friendly_id have
    all been added to it), so the copies are what a signature change has to chase.
    Pass constructor kwargs through; layer test-specific state on the result.
    """
    from unittest.mock import AsyncMock, MagicMock

    from services.draft_setup_manager import DraftSetupManager

    mgr = DraftSetupManager(session_id="s", draft_id="d", cube_id="c", guild_id="g", **kwargs)
    mgr.socket_client = MagicMock()
    mgr.socket_client.connected = True
    mgr.socket_client.emit = AsyncMock(return_value=True)
    return mgr


@pytest_asyncio.fixture
async def live_views():
    """Track discord.ui.View instances and stop them when the test ends.

    A View started under a running loop spawns a timeout task; leaving it running
    makes pytest report pending tasks at teardown, from whichever test happens to be
    last. Async on purpose — View.stop() cancels that task, so teardown has to happen
    while the loop is still open.

    Written out three times across the view-dispatch tests before it landed here.
    Yields a track(view) callable that returns the view, so it reads inline:
    ``view = track(SomeView(...))``.
    """
    tracked = []

    def track(view):
        tracked.append(view)
        return view

    yield track
    for view in tracked:
        view.stop()


def make_view_store():
    """A bare py-cord ViewStore for exercising real dispatch registration.

    The ConnectionState it normally holds is only needed for actually dispatching an
    interaction, so a stand-in is enough for tests about the registration keyspace.
    """
    from types import SimpleNamespace

    from discord.ui.view import ViewStore

    return ViewStore(state=SimpleNamespace())
