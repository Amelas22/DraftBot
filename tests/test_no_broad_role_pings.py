"""No AllowedMentions in bot code may ping every role or @everyone.

`roles=True` lets any role mention in the message notify, including one a user
smuggled in through a team name or display name. Messages that mean to ping a
role must name it with helpers.mentions.pings(roles=[...]).
"""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".claude", "alembic", "scripts", "tests", ".git", ".superpowers"}

# (file, enclosing function) pairs that may hold a broad AllowedMentions.
ALLOWED = {
    # An edit, which never notifies; it only adds mentioned members to a thread.
    ("helpers/utils.py", "send_then_mention"),
}


def _is_true(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _broad_sites(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    sites: list[tuple[int, str]] = []

    def visit(node: ast.AST, func: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        if isinstance(node, ast.Call):
            callee = node.func
            name = (callee.attr if isinstance(callee, ast.Attribute)
                    else callee.id if isinstance(callee, ast.Name) else "")
            if name == "AllowedMentions" and any(
                    kw.arg in ("roles", "everyone") and _is_true(kw.value)
                    for kw in node.keywords):
                sites.append((node.lineno, func))
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    visit(tree, "")
    return sites


# Task 2 converts the remaining sites to helpers.mentions.pings and drops this.
@pytest.mark.xfail(strict=True, reason="Task 2")
def test_no_broad_allowed_mentions():
    offenders = []
    for path in sorted(REPO.rglob("*.py")):
        rel = path.relative_to(REPO)
        if SKIP_DIRS & set(rel.parts):
            continue
        for line, func in _broad_sites(path):
            if (rel.as_posix(), func) not in ALLOWED:
                offenders.append(f"{rel.as_posix()}:{line}")
    assert not offenders, (
        "AllowedMentions with roles=True/everyone=True; use "
        "helpers.mentions.pings(roles=[...]): " + ", ".join(offenders))
