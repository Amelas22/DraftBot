#!/usr/bin/env python3
"""Run the PR-readiness passes and record a receipt for the current commit.

`.claude/hooks/require_pr_readiness.py` refuses the commands that open a PR until
a receipt exists for the exact HEAD sha.

Two kinds of check live here, and the split is deliberate:

  * pytest and pyrefly this script RUNS, every time. Their results are measured,
    not claimed. There is no flag to reuse an earlier result and no fast path:
    receipts are user-writable files, so trusting one to stand in for a real run
    would let the recorder launder an assertion into a real-looking measurement.
  * simplify, review and e2e it cannot run for you -- they need judgement, a
    second model, and a live Discord guild. Those are attested, and each demands
    a note saying what was actually done. A receipt is not a checkbox; the note
    is what a reviewer reads when they want to know whether the pass was real.

The receipt is keyed by commit sha. Amending or adding a commit afterwards
invalidates it, because the thing that was reviewed no longer exists.

    pipenv run python scripts/pr_readiness.py \\
        --simplify "4 agents; one reuse finding, fixed" \\
        --review   "codex: 2 findings, thread-name collision fixed" \\
        --e2e      "tournament-scouting-pools: PASS"
"""
import argparse
import importlib.util
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
RECEIPTS = ROOT / ".claude" / "pr-readiness"


def _gate() -> ModuleType:
    """The hook module, loaded by path -- it defines what a complete receipt is,
    so this script asks it rather than restating it.

    Says so plainly if the hook is missing: without the check that is an
    AttributeError on None several lines later, which reads like a bug here
    rather than a file that is not where it should be.
    """
    path = ROOT / ".claude" / "hooks" / "require_pr_readiness.py"
    spec = importlib.util.spec_from_file_location("require_pr_readiness", path)
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load the readiness hook at {path}; is it still there?")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_GATE = _gate()
MEASURED: tuple[str, ...] = _GATE.MEASURED
ATTESTED: tuple[str, ...] = _GATE.ATTESTED


def _run(label: str, cmd: list[str],
         ok_when: Callable[[int, str], dict[str, Any]]) -> dict[str, Any]:
    """Run a check, print it live, and summarise the result."""
    print(f"\n=== {label}: {' '.join(cmd)}")
    started = time.time()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    took = round(time.time() - started, 1)
    result = ok_when(proc.returncode, (proc.stdout or "") + (proc.stderr or ""))
    print(f"    {label}: {result['summary']}  ({took}s)")
    return {**result, "duration_s": took, "command": " ".join(cmd)}


def _pytest_result(code: int, out: str) -> dict[str, Any]:
    import re
    m = re.search(r"^(\d+ (?:passed|failed).*)$", out, re.M)
    return {"ok": code == 0, "summary": m.group(1).strip() if m else f"exit {code}"}


def _pyrefly_result(code: int, out: str) -> dict[str, Any]:
    import re
    m = re.search(r"(\d+) errors?", out)
    errors = int(m.group(1)) if m else None
    checked = re.search(r"(\d+) files?", out)
    # 0 errors over 0 files is what a worktree reports when pyrefly resolves the
    # wrong interpreter (see CLAUDE.md "Type Checking"). A green result that
    # checked nothing is the one answer this must never record as a pass.
    if checked and checked.group(1) == "0":
        return {"ok": False, "summary": "checked 0 files (wrong interpreter?)"}
    return {"ok": code == 0 and errors == 0,
            "summary": f"{errors} errors" if errors is not None else f"exit {code}"}


def _interpreter() -> str:
    """pyrefly needs this spelled out: from a git worktree a bare run resolves
    system site-packages, checks nothing, and reports success."""
    return subprocess.run(["pipenv", "--py"], cwd=ROOT, capture_output=True,
                          text=True, check=True).stdout.strip()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Derived from the gate's list, so a pass added there gets a flag here
    # instead of silently having nowhere to be recorded.
    for name in ATTESTED:
        ap.add_argument(f"--{name}", metavar="NOTE",
                        help=f"what the {name} pass found, and what you did about it")
    args = ap.parse_args()

    notes = {name: (getattr(args, name) or "").strip() for name in ATTESTED}
    missing = [f"--{n}" for n, v in notes.items() if not v]
    if missing:
        sys.exit(f"Refusing to write a receipt without {', '.join(missing)}.\n"
                 f"These passes cannot be run from here, so the note IS the evidence. "
                 f"Run them, then say what happened.")

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout.strip()
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True, check=True).stdout.strip()
    # Untracked files are deliberately ignored: this repo's e2e harness is
    # untracked by design, so requiring a spotless tree would make the recorder
    # unrunnable. Tracked changes are the ones that would ship unreviewed.
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                           cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if dirty:
        sys.exit("Uncommitted changes to tracked files. Commit them first -- a\n"
                 "receipt names a commit, and what you tested must BE that commit.\n\n"
                 + dirty)

    checks: dict[str, Any] = {
        "pytest": _run("pytest", ["pipenv", "run", "python", "-m", "pytest", "-q"],
                       _pytest_result),
        "pyrefly": _run("pyrefly", ["pipenv", "run", "pyrefly", "check",
                                    "--python-interpreter-path", _interpreter()],
                        _pyrefly_result),
    }
    for name in ATTESTED:
        checks[name] = {"ok": True, "attested": True, "note": notes[name]}

    RECEIPTS.mkdir(parents=True, exist_ok=True)
    path = RECEIPTS / f"{head}.json"
    path.write_text(json.dumps(
        {"sha": head, "branch": branch,
         "recorded_at": datetime.now().isoformat(timespec="seconds"),
         "checks": checks}, indent=2) + "\n")

    failed = [n for n, c in checks.items() if not c.get("ok")]
    print(f"\nreceipt: {path.relative_to(ROOT)}")
    if failed:
        print(f"NOT READY -- failing: {', '.join(failed)}")
        print("The hook will keep refusing until these pass.")
        return 1
    print(f"ready: opening a PR is unblocked for {head[:8]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
