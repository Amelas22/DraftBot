"""What must be clickable first after a restart.

on_ready restores persistent views in the order it calls them, and one of those
calls is slow: re_register_views ends by walking every old quiz session to unpin
it. In production that pass was 959 messages, all failing on Manage Messages in
one channel, at roughly 39 a minute -- half an hour during which every call
after it had not run yet.

The tournament control view is the Start Draft button on a live match. While its
registration waited behind that pass, Discord delivered the click to a bot with
no handler for the custom_id, and the player saw "the bot didn't respond" with
nothing logged at all.

Asserted against the source rather than by running on_ready: it needs a live
gateway, and the invariant here is the ORDER of the calls, which is exactly what
the source says.
"""
import ast
import inspect
from pathlib import Path


def _on_ready_calls():
    """The awaited/called function names inside bot.py's on_ready, in order."""
    tree = ast.parse(Path("bot.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_ready":
            names = []
            for call in [n for n in ast.walk(node) if isinstance(n, ast.Call)]:
                func = call.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name:
                    names.append((call.lineno, name))
            return [name for _, name in sorted(names)]
    raise AssertionError("bot.py has no on_ready")


def test_match_buttons_are_registered_before_the_quiz_sweep():
    """A live match's Start Draft button must not wait on quiz maintenance."""
    calls = _on_ready_calls()
    assert "re_register_tournament_views" in calls, calls
    assert "re_register_views" in calls, calls
    assert calls.index("re_register_tournament_views") < calls.index("re_register_views"), (
        "re_register_views ends with a per-quiz unpin sweep that took ~30 minutes in "
        "production; registering match control views after it leaves the Start Draft "
        "button dead for that whole window"
    )


def test_every_cheap_view_registration_precedes_the_quiz_sweep():
    """The same argument covers the other restorations, which are all far cheaper
    than the sweep and all currently queued behind it."""
    calls = _on_ready_calls()
    sweep = calls.index("re_register_views")
    for name in ("re_register_live_drafts", "re_register_tournament_views",
                 "re_register_premade_nudges"):
        assert name in calls, f"{name} is no longer restored at startup: {calls}"
        assert calls.index(name) < sweep, f"{name} still waits on the quiz sweep"
