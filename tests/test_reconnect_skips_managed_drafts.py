"""A gateway reconnect must not duplicate what startup already started.

Discord refires on_ready on every gateway reconnect, not just at startup. It
used to start another copy of each background loop every time, and to build a
manager for every draft still in setup beside the one it already had -- in
production one queue ended up with three, all connected to Draftmancer as the
same user, the older two out of reach of anything that stops a draft's manager.
The loops are now started once and revived only if they die, so they must also
survive a failed pass rather than die from it.
"""
import pytest

from conftest import make_manager, run_until_sleep, seed_queue
from reconnect_drafts import reconnect_draft_setup_sessions
from services.draft_setup_manager import ACTIVE_MANAGERS


pytestmark = pytest.mark.usefixtures("clean_manager_registry")


@pytest.mark.asyncio
async def test_a_draft_that_already_has_a_manager_keeps_it(test_db):
    await seed_queue("managed")
    running = make_manager(session_id="managed", draft_id="d-managed")

    built = await reconnect_draft_setup_sessions(None)

    assert built == [], "a second manager was built for a draft that has one"
    assert ACTIVE_MANAGERS["managed"] is running


@pytest.mark.asyncio
async def test_a_draft_without_a_manager_still_gets_one(test_db):
    """The restart case this function exists for must keep working."""
    await seed_queue("orphaned")

    built = await reconnect_draft_setup_sessions(None)

    assert [m.session_id for m in built] == ["orphaned"]
    assert ACTIVE_MANAGERS["orphaned"] is built[0]


def _on_ready():
    import ast
    from pathlib import Path

    tree = ast.parse(Path("bot.py").read_text())
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "on_ready")


def _names_called_inside(nodes):
    import ast

    return {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
            for node in nodes for c in ast.walk(node) if isinstance(c, ast.Call)}


def test_on_ready_starts_each_loop_through_ensure_running():
    """Each is a loop meant to run once per process. Started unconditionally,
    every gateway reconnect added a copy; ensure_running starts one only if
    none is running, which also revives one that died.

    Asserted against the source: on_ready needs a live gateway to run.
    """
    import ast

    starts = [c for c in ast.walk(_on_ready()) if isinstance(c, ast.Call)
              and getattr(c.func, "id", None) == "ensure_running"]
    started = _names_called_inside(starts)
    for name in ("cleanup_sessions_task", "check_inactive_players_task",
                 "run_log_reconciler", "watch_serve_health", "lending_jobs_watchdog"):
        assert name in started, f"{name} is not started through ensure_running"


def test_on_ready_reconnects_setup_drafts_behind_a_guard():
    """Startup reconnection is a one-off, not a loop: it must not rerun on every
    gateway reconnect, which only leaves the managers it built running."""
    import ast

    guarded = _names_called_inside(n for n in ast.walk(_on_ready()) if isinstance(n, ast.If))
    assert "reconnect_draft_setup_sessions" in guarded


@pytest.mark.asyncio
async def test_a_failed_cleanup_pass_does_not_end_the_cleanup_loop(test_db):
    """A loop that dies is only revived at the next gateway reconnect, and one
    bad pass -- a locked database, say -- used to end it there and then."""
    from unittest.mock import MagicMock, patch

    import utils

    real_session = utils.AsyncSessionLocal
    opened = []

    def flaky_session(*args, **kwargs):
        opened.append(1)
        if len(opened) == 1:
            raise RuntimeError("database is locked")
        return real_session(*args, **kwargs)

    with patch("utils.AsyncSessionLocal", flaky_session):
        await run_until_sleep(utils.cleanup_sessions_task(MagicMock()), 600, nth=2)

    assert len(opened) == 2, "the loop did not survive its failed pass"


@pytest.mark.asyncio
async def test_a_failed_inactivity_pass_does_not_end_the_inactivity_loop():
    """The same, for the weekly inactive-player check."""
    from types import SimpleNamespace

    import utils

    class Unreachable:
        def __iter__(self):
            passes.append(1)
            raise RuntimeError("guild cache unavailable")

    passes = []
    bot = SimpleNamespace(guilds=Unreachable())

    await run_until_sleep(utils.check_inactive_players_task(bot), 7 * 24 * 60 * 60, nth=3)

    assert len(passes) == 2, "the loop did not survive its failed pass"
