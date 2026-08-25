"""create_team_channel must actually call the scouting-thread helper, with the
right rosters, after the channel row is committed.

tests/test_opponent_threads.py covers the helper in isolation -- every one of
those tests still passes if the call site is deleted, the rosters are swapped,
or the call is moved ahead of the commit that persists channel_ids and the
'pairings' stage. This file is what notices.

Uses the shared harness from conftest. It used to carry its own copy of these
fakes, which broke twice in one week when create_team_channel grew a dependency
while the suites on the shared harness stayed green.
"""
import pytest
import pytest_asyncio

import views
from conftest import make_channel_harness


@pytest_asyncio.fixture
async def wired(monkeypatch):
    """Returns (view, guild, calls, spawn) -- `calls` records the ordering of the
    DB commit against the helper call."""
    view, guild, db = make_channel_harness(monkeypatch, session_type="team")

    async def spawn(*args, **kwargs):
        db.calls.append("spawn")
        spawn.args = args
        return 0

    spawn.args = None
    monkeypatch.setattr(views, "spawn_opponent_threads", spawn)
    return view, guild, db.calls, spawn


@pytest.mark.asyncio
async def test_red_team_channel_spawns_threads_for_team_b(wired):
    view, guild, _calls, spawn = wired
    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    channel, team_name, team_a, team_b, sign_ups = spawn.args
    assert team_name == "Red-Team"
    assert team_a == ["a1"]
    assert team_b == ["b1"]
    assert sign_ups == {"a1": "Alice", "b1": "Dave"}
    # Discord lowercases text channel names, and the shared double models that.
    assert channel.name.lower() == "red-team-chat-abc1"


@pytest.mark.asyncio
async def test_threads_are_spawned_after_the_channel_row_is_committed(wired):
    """channel_ids and session_stage='pairings' are the draft's critical path;
    the threads are a nice-to-have that must not precede them."""
    view, guild, calls, _spawn = wired
    await view.create_team_channel(guild, "Red-Team", [], ["a1"], ["b1"])

    assert "spawn" in calls, "create_team_channel never called spawn_opponent_threads"
    assert calls.index("db-commit") < calls.index("spawn")


@pytest.mark.asyncio
async def test_shared_draft_channel_still_calls_the_helper_to_no_op(wired):
    """The 'Draft' channel is filtered inside the helper (opponent_ids returns
    []), not at the call site -- keep that contract explicit."""
    view, guild, _calls, spawn = wired
    await view.create_team_channel(guild, "Draft", [], ["a1"], ["b1"])

    assert spawn.args is not None
    assert spawn.args[1] == "Draft"
