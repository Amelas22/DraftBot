"""Every name a module loads is a name that module binds.

A refactor that rewrites literals into constants can land the rewrites and
silently skip the import that defines them -- the module still parses, still
imports, and every test that never calls the one changed function still
passes. The failure surfaces in production, as a NameError from a code path
nobody unit-tests.

That shipped: `create_rooms_pairings` referenced RED_SIDE, BLUE_SIDE and
SHARED_CHAT_TEAM without importing them, which broke draft room creation
outright. pyrefly would have caught it, but views.py is not in pyrefly.toml's
project-includes and the file is far too large to opt in casually.

This is the cheap subset of what a type checker does, applied to every tracked
file regardless of the pyrefly backlog.
"""
import ast
import builtins
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

MODULE_DUNDERS = {"__name__", "__file__", "__doc__", "__spec__",
                  "__package__", "__loader__", "__builtins__", "__debug__"}


def undefined_names(source: str) -> dict[str, list[int]] | None:
    """Names loaded somewhere in this module that it binds nowhere.

    Deliberately scope-blind: a binding anywhere in the module counts as a
    binding everywhere, so a name assigned only inside some other function
    reads as defined. That under-reports on purpose. This check is meant to
    gate every commit, so a false positive -- a passing file it calls broken --
    would get it disabled within a week; missing a subtler scope bug is the
    acceptable half of that trade. What it cannot miss is a name that appears
    nowhere in the file, which is exactly what a dropped import leaves behind.

    Returns None for a module with a star import, whose bindings can't be known
    from the source alone.
    """
    tree = ast.parse(source)
    if any(alias.name == "*"
           for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
           for alias in node.names):
        return None

    bound = set(dir(builtins)) | MODULE_DUNDERS
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)

    found: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and node.id not in bound:
            found.setdefault(node.id, []).append(node.lineno)
    return found


def _tracked_python_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO,
                         capture_output=True, text=True, check=True).stdout
    return [REPO / line for line in out.split()]


def test_no_tracked_module_uses_a_name_it_never_defines():
    """The guard itself. One entry here is a module that raises NameError as
    soon as the offending line runs."""
    offenders = []
    for path in _tracked_python_files():
        result = undefined_names(path.read_text())
        if not result:
            continue
        for name, lines in sorted(result.items()):
            rel = path.relative_to(REPO)
            offenders.append(f"{rel}:{lines[0]} uses undefined {name!r}")

    assert not offenders, (
        "These modules load names they never bind (missing import?):\n  "
        + "\n  ".join(offenders))


def test_the_guard_actually_detects_a_dropped_import():
    """Without this, deleting the body of undefined_names would leave the guard
    above passing forever. It is asserting the absence of something, so it has
    to prove it can still see that something present.

    The planted bug is the real one: a helpers.draft_rooms import that lists
    some names and omits the rest.
    """
    dropped_import = (
        "from helpers.draft_rooms import DRAFT_ROOM_COUNT\n"
        "def create_rooms_pairings():\n"
        "    return DRAFT_ROOM_COUNT, RED_SIDE, SHARED_CHAT_TEAM\n"
    )
    assert undefined_names(dropped_import) == {"RED_SIDE": [3], "SHARED_CHAT_TEAM": [3]}


@pytest.mark.parametrize("source, why", [
    ("import os\nx = os.getcwd()\n", "plain import"),
    ("from a import b as c\nx = c\n", "aliased import"),
    ("x = [i * 2 for i in range(3)]\n", "comprehension target"),
    ("def f(a, *, b=1, **kw):\n    return a, b, kw\n", "every kind of parameter"),
    ("try:\n    pass\nexcept ValueError as e:\n    print(e)\n", "except-as binding"),
    ("def f():\n    global g\n    g = 1\ndef h():\n    return g\n", "global binding"),
    ("if (n := 5) > 1:\n    print(n)\n", "walrus binding"),
    ("class C:\n    def m(self):\n        return C\n", "class name in its own body"),
    ("print(len(str(1)))\n", "builtins"),
])
def test_ordinary_bindings_are_not_reported(source, why):
    """The false positives that would make this check unusable. A guard people
    turn off protects nothing, so the binding forms real code uses every day
    are pinned here rather than discovered in a red build."""
    assert undefined_names(source) == {}, why


def test_a_star_import_is_skipped_rather_than_guessed():
    """`from x import *` binds names this file cannot see. Reporting them all
    as undefined would be the false-positive flood; skipping is honest."""
    assert undefined_names("from os.path import *\nx = join('a', 'b')\n") is None
