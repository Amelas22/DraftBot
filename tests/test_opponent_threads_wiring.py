"""create_team_channel must actually call the scouting-thread helper, with the
right rosters, after the channel row is committed.

tests/test_opponent_threads.py covers the helper in isolation -- every one of
those tests still passes if the call site is deleted, the rosters are swapped,
or the call is moved ahead of the commit that persists channel_ids and the
'pairings' stage. This file is what notices.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

import views


class _ACM:
    """Minimal async context manager returning `value`."""

    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


class FakeDB:
    def __init__(self, calls):
        self.calls = calls

    def begin(self):
        return _ACM(self)

    async def execute(self, *args, **kwargs):
        self.calls.append("db-execute")

    async def commit(self):
        self.calls.append("db-commit")


class FakeRole:
    """Hashable stand-in -- overwrites is a dict keyed by role/member objects,
    and SimpleNamespace defines __eq__ so it can't be a key."""

    def __init__(self, name):
        self.name = name
        self.tags = None


class FakeGuild:
    id = 4242
    name = "Test Guild"
    categories: list = []
    roles: list = []

    def __init__(self, channel):
        self.me = FakeRole("bot")
        self.default_role = FakeRole("everyone")
        self._channel = channel

    async def create_text_channel(self, **kwargs):
        return self._channel


@pytest_asyncio.fixture
async def wired(monkeypatch):
    """create_team_channel with its config/DB/Discord edges faked out.

    Returns (view, guild, calls, spawn) -- `calls` records the ordering of the
    DB commit against the helper call.
    """
    calls: list = []
    channel = SimpleNamespace(id=999, name="Red-Team-Chat-abc1", threads=[])

    monkeypatch.setattr("config.get_config", lambda gid: {
        "categories": {"draft": "Drafts"}, "roles": {"admin": "Admin"},
    })
    monkeypatch.setattr("config.is_special_guild", lambda gid: False)
    monkeypatch.setattr("config.get_bots_with_draft_access", lambda gid: [])

    session = SimpleNamespace(
        friendly_id="abc1",
        premade_match_id=None,
        session_type="team",
        sign_ups={"a1": "Alice", "b1": "Dave", "b2": "Erin"},
        team_a=["a1"],
        team_b=["b1", "b2"],
    )
    monkeypatch.setattr(views, "get_draft_session", AsyncMock(return_value=session))
    monkeypatch.setattr(views, "AsyncSessionLocal", lambda: _ACM(FakeDB(calls)))

    async def spawn(*args, **kwargs):
        calls.append("spawn")
        spawn.args = args
        return 0

    spawn.args = None
    monkeypatch.setattr(views, "spawn_opponent_threads", spawn)

    view = views.PersistentView(bot=None, draft_session_id="s1", session_type="team")
    # Set by the "Draft" channel pass, which both call sites run first.
    view.draft_chat_channel = None
    return view, FakeGuild(channel), calls, spawn


@pytest.mark.asyncio
async def test_red_team_channel_spawns_threads_for_team_b(wired):
    view, guild, _calls, spawn = wired
    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1", "b2"])

    channel, team_name, team_a, team_b, sign_ups = spawn.args
    assert team_name == "Red-Team"
    assert team_a == ["a1"]
    assert team_b == ["b1", "b2"]
    assert sign_ups == {"a1": "Alice", "b1": "Dave", "b2": "Erin"}
    assert channel.name == "Red-Team-Chat-abc1"


@pytest.mark.asyncio
async def test_threads_are_spawned_after_the_channel_row_is_committed(wired):
    """channel_ids and session_stage='pairings' are the draft's critical path;
    the threads are a nice-to-have that must not precede them."""
    view, guild, calls, _spawn = wired
    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1", "b2"])

    assert "spawn" in calls, "create_team_channel never called spawn_opponent_threads"
    assert calls.index("db-commit") < calls.index("spawn")


@pytest.mark.asyncio
async def test_shared_draft_channel_still_calls_the_helper_to_no_op(wired):
    """The 'Draft' channel is filtered inside the helper (opponent_ids returns
    []), not at the call site -- keep that contract explicit."""
    view, guild, _calls, spawn = wired
    await view.create_team_channel(guild, "Draft", [], ["a1"], ["b1", "b2"])

    assert spawn.args is not None
    assert spawn.args[1] == "Draft"
