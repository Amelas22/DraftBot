"""A gateway reconnect must not duplicate what startup already started.

Discord refires on_ready on every gateway reconnect, not just at startup. It
used to start another copy of each background loop every time, and to build a
manager for every draft still in setup beside the one it already had -- in
production one queue ended up with three, all connected to Draftmancer as the
same user, the older two out of reach of anything that stops a draft's manager.
"""
import pytest

from conftest import make_manager, seed_queue
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
