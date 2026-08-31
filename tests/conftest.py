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

make_channel_harness: create_team_channel with its config, DB and Discord
edges faked out -- import it (``from conftest import make_channel_harness``)
instead of hand-writing another guild double. Returns (view, guild, db); the
db records what was persisted, which is what session cleanup actually reads.
"""
import os
import random
import tempfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
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
                       cube="TestCube", draft_chat_channel=None,
                       channel_ids=None, draft_id=None, rooms_created_at=None):
    """Seed one DraftSession plus its MatchResults.

    teams: (team_a_list, team_b_list) or None (legacy-style, no team JSON).
    matches: iterable of (player1, player2, winner, submitted_at_or_None).
    draft_chat_channel / channel_ids: the draft's rooms, for tests that resolve a
    session from a channel. Note the type asymmetry production stores them with --
    the chat as a string, channel_ids as JSON ints.
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
            draft_chat_channel=draft_chat_channel,
            channel_ids=channel_ids,
            draft_id=draft_id, rooms_created_at=rooms_created_at,
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

    # Defaults, not hardcoded arguments: the docstring promises callers can pass
    # constructor kwargs, and passing session_id/draft_id/guild_id used to raise
    # "got multiple values" instead of overriding.
    args = {"session_id": "s", "draft_id": "d", "cube_id": "c", "guild_id": "g"}
    args.update(kwargs)
    mgr = DraftSetupManager(**args)
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


# --- create_team_channel harness ---------------------------------------------
# Shared because more than one suite drives create_team_channel, and a third
# file already hand-rolled the same fakes once. Every dependency that method
# acquires has to be patched somewhere; one home means one place to update it.

class _ACM:
    """Minimal async context manager returning `value`."""

    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


class RecordingDB:
    """Records the UPDATE create_team_channel issues, so a test can assert on what
    is PERSISTED rather than on an in-memory list -- the cleanup sweep reads the
    stored channel_ids and nothing else. `calls` records ordering, for the suite
    that cares whether the commit preceded the opponent-thread spawn."""

    def __init__(self):
        self.persisted = {}
        self.calls: list = []

    def begin(self):
        return _ACM(self)

    async def execute(self, statement, *args, **kwargs):
        self.calls.append("db-execute")
        self.persisted.update(statement.compile().params)

    async def commit(self):
        self.calls.append("db-commit")


class HarnessRole:
    """Hashable stand-in -- overwrites is keyed by role/member objects, and
    SimpleNamespace defines __eq__ so it cannot be a key."""

    def __init__(self, name):
        self.name = name
        self.tags = None


class HarnessCategory:
    """A category with an occupancy, because capacity is what decides where a
    draft's rooms go -- a double carrying only a name cannot express "nearly
    full", which is the case that used to split a draft across two of them."""

    def __init__(self, name, position=0, children=0):
        self.name = name
        self.position = position
        self.overwrites = {}
        self.channels = [object()] * children


class HarnessChannel:
    _next_id = 1000

    def __init__(self, name, kind, category=None):
        HarnessChannel._next_id += 1
        self.id = HarnessChannel._next_id
        self.name = name
        self.kind = kind
        self.category = category


class HarnessGuild:
    """A guild real enough for create_team_channel's channel creation."""

    id = 4242
    name = "Test Guild"
    roles: list = []

    def __init__(self, categories=(), voice_error=None):
        self.me = HarnessRole("bot")
        self.default_role = HarnessRole("everyone")
        self.categories = list(categories)
        self.text_calls = []
        self.voice_calls = []
        self.voice_error = voice_error
        # What the guild actually holds. Room creation looks here to decide
        # whether a previous run already made a channel, so a double that did not
        # remember its own creations could never exercise the resume path.
        self.existing = []

    @property
    def text_channels(self):
        return [c for c in self.existing if c.kind == "text"]

    @property
    def voice_channels(self):
        return [c for c in self.existing if c.kind == "voice"]

    def seed(self, name, kind="text", category=None):
        """Register a channel in the guild -- both for one left behind by a
        previous run and for the ones this run creates."""
        # Discord lowercases text channel names and leaves voice names alone.
        stored = name.lower() if kind == "text" else name
        channel = HarnessChannel(stored, kind, category)
        self.existing.append(channel)
        return channel

    async def create_text_channel(self, **kwargs):
        self.text_calls.append(kwargs)
        return self.seed(kwargs["name"], "text", kwargs.get("category"))

    async def create_voice_channel(self, **kwargs):
        self.voice_calls.append(kwargs)
        if self.voice_error:
            raise self.voice_error
        return self.seed(kwargs["name"], "voice", kwargs.get("category"))

    async def create_category(self, name, overwrites=None, position=0):
        # Room creation overflows into a NEW category when the configured one
        # cannot hold the whole draft, so the double has to be able to make one.
        made = HarnessCategory(name, position)
        made.overwrites = overwrites or {}
        self.categories.append(made)
        return made


def make_channel_harness(monkeypatch, *, categories=("Draft Channels",), features=None,
                         extra_config=None, session_type="premade",
                         seeded=(), strays=(), **guild_kwargs):
    """create_team_channel with its config, DB and Discord edges faked out.

    Returns (view, guild, db). `features` seeds the config's feature flags so the
    real readers run against them rather than being stubbed out themselves.

    `seeded` is [(name, kind)] left behind by an earlier run of THIS draft: they
    go into the guild AND into the session's channel_ids, which is what makes
    them reusable. `strays` go into the guild only -- same name, not this draft's
    -- which is the case that must never be adopted.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import views

    cats = {"draft": "Draft Channels"}
    cats.update(extra_config or {})
    monkeypatch.setattr(
        "config.get_config",
        lambda gid: {"categories": cats, "roles": {"admin": "Admin"},
                     "features": features or {}},
    )
    monkeypatch.setattr("config.get_bots_with_draft_access", lambda gid: [])

    session = SimpleNamespace(
        friendly_id="abc1",
        channel_ids=[],
        session_type=session_type,
        sign_ups={"a1": "Alice", "b1": "Dave"},
        team_a=["a1"],
        team_b=["b1"],
    )
    monkeypatch.setattr(views, "get_draft_session", AsyncMock(return_value=session))
    db = RecordingDB()
    monkeypatch.setattr(views, "AsyncSessionLocal", lambda: _ACM(db))
    monkeypatch.setattr(views, "spawn_opponent_threads", AsyncMock(return_value=0))

    view = views.PersistentView(bot=None, draft_session_id="s1", session_type=session_type)
    view.draft_chat_channel = None
    guild = HarnessGuild(
        [c if hasattr(c, "channels") else HarnessCategory(getattr(c, "name", c))
         for c in categories],
        **guild_kwargs)
    session.channel_ids = [guild.seed(name, kind).id for name, kind in seeded]
    for name, kind in strays:
        guild.seed(name, kind)
    return view, guild, db


def make_draft_stub(session_stage="pairings", sign_ups=None, **overrides):
    """A DraftSession double carrying what /scrap, /abandon and is_finished_draft
    read off a session.

    The victory-message fields matter: a played draft usually still sits at
    'pairings', so those are the real completion markers (helpers.stale_drafts.
    is_finished_draft). A stub missing them cannot exercise the completion guard
    at all -- it raises instead, which is how their absence was first noticed.
    """
    base = dict(
        session_id="sess_123", session_stage=session_stage,
        draft_chat_channel=None, channel_ids=[],
        sign_ups=sign_ups or {"1": "One", "2": "Two"},
        victory_message_id_draft_chat=None,
        victory_message_id_results_channel=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def sent_to_invoker(ctx):
    """Everything a command replied to the person who ran it, as one string.

    Shared so that widening it -- to read embeds, or kwargs-passed content --
    widens what BOTH command suites can see, rather than one of them silently
    keeping the narrower notion of "what did it say".
    """
    return " ".join(str(c.args[0]) for c in ctx.followup.send.await_args_list if c.args)


@pytest.fixture
def draft_control_cog():
    """The cog under test for /scrap and /abandon."""
    from cogs.draft_control import DraftControlCog
    return DraftControlCog(bot=MagicMock())
