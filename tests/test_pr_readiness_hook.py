"""The gate that stops a PR going out unreviewed.

This exists because of 2026-09-04: three PRs opened in one session, and the
readiness passes -- simplify, an adversarial review, an e2e run against a real
guild -- were run for one of them. Not refused, not argued down. Forgotten. On
the PR where the review did run it found a real defect in the first ten minutes,
and the work that skipped them shipped a NameError that broke draft room
creation outright.

So the failure being designed against is an omission under momentum. That is
what every trade here answers to:

  * It must not miss an ordinary command. A gate a stray leading space defeats
    is not a gate, because the space is an accident, not an attack.
  * It must not fire on ordinary work. A gate that blocks `git commit -m` while
    you write ABOUT it, or blocks writing a file that quotes it, gets switched
    off within a day and then protects nothing.
  * It is not a sandbox. A GraphQL mutation, an MCP tool, or the web UI opens a
    PR without a Bash call to inspect. Guarding evasion needs a server-side
    check; this guards forgetting.

The matcher tokenises rather than pattern-matching raw text, which is what makes
`  gh pr create` the same as `gh pr create` while `git commit -m "gh pr create"`
is not. Each case below is one an earlier version got wrong.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / ".claude" / "hooks" / "require_pr_readiness.py"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load(HOOK, "require_pr_readiness")
recorder = _load(REPO / "scripts" / "pr_readiness.py", "pr_readiness")
PASSES = gate.REQUIRED


def run_hook(command, project_dir, env_extra=None, cwd=None):
    """Feed the hook a PreToolUse payload; give back its decision, or None."""
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project_dir))
    env.pop("PR_READINESS_OVERRIDE", None)
    env.update(env_extra or {})
    payload = {"tool_input": {"command": command}}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"] if proc.stdout.strip() else None


@pytest.fixture
def repo(tmp_path):
    """A real git repo, because the hook resolves HEAD with git."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=tmp_path, check=True)
    return tmp_path


def head_of(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()


def write_receipt(repo, sha=None, checks=None):
    d = repo / ".claude" / "pr-readiness"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sha or head_of(repo)}.json").write_text(json.dumps(
        {"checks": checks if checks is not None else {n: {"ok": True} for n in PASSES}}))


# --- what counts as opening a PR -------------------------------------------
# These run against the matcher directly. Every one was a verified bypass of an
# earlier version: a gutted matcher passed the whole suite before they existed.

@pytest.mark.parametrize("command, why", [
    ("gh pr create --fill", "the plain form"),
    ("   gh pr create --fill", "a stray leading space -- an accident, not an attack"),
    ("\tgh pr create --fill", "a leading tab"),
    ("gh pr ready 491", "marking one ready is the same claim"),
    ("GH_REPO=a/b gh stack submit", "an env prefix stays command position"),
    ('TITLE="a b" gh pr create', "a quoted env value spans the space"),
    ("command gh pr create", "the `command` builtin"),
    ("/usr/bin/gh pr create", "an absolute path"),
    ("\\gh pr create", "a backslash defeats an alias, not this"),
    ("env gh pr create", "env as a wrapper"),
    ("nohup gh pr create", "nohup"),
    ("time gh pr create", "time"),
    ('bash -c "gh pr create --fill"', "a nested shell"),
    ("`gh pr create --fill`", "backtick substitution runs it"),
    ("$(gh pr create --fill)", "dollar-paren substitution runs it"),
    ("{ gh pr create --fill; }", "a brace group"),
    ("if true; then gh pr create --fill; fi", "inside if/then"),
    ("for i in 1; do gh pr create; done", "inside for/do"),
    ("true; gh pr create --fill", "after a semicolon"),
    ("echo x | gh pr create --fill", "after a pipe"),
    ("(gh pr create --fill)", "in a subshell"),
    ("gh \\\n  pr create --fill", "split over a line continuation"),
    ("cat <<'EOF' > f 2>&1\nbody\nEOF\ngh pr create", "a real command AFTER a heredoc"),
    ('grep -n "<<EOF" doc.md\ngh pr create\nEOF is the tag',
     "a quoted <<TAG must not open a strip region that hides the next line"),
    ('echo "note <<EOF"\ngh pr create\nEOF=done', "the same, without stderr noise"),
    ("gh api repos/o/r/pulls -X POST -f head=b", "the REST spelling"),
    ("gh api repos/o/r/pulls -F title=x", "gh switches to POST on any field flag"),
    ("gh api repos/o/r/pulls --input body.json", "a body file is a write too"),
])
def test_these_open_a_pr(command, why):
    assert gate.opens_a_pr(command) is True, why


@pytest.mark.parametrize("command, why", [
    ("git push -u origin my-branch", "pushing a branch claims nothing"),
    ("gh pr view 491", "reading"),
    ("gh pr list", "listing"),
    ("gh pr merge 488 --squash", "merging one a human already approved"),
    ("gh api repos/o/r/pulls", "listing PRs over the API"),
    ("gh api repos/o/r/pulls/491", "reading one"),
    ("gh api repos/o/r/issues -X POST -f title=x", "a write, but not a PR"),
    ("echo gh pr create", "naming it"),
    ('echo "gh pr create" >> notes.md', "writing it to a file"),
    ('grep -rn "gh pr create" .', "searching for it"),
    ('git commit -m "add (gh pr create) gate"', "a commit message with parens"),
    ('git commit -m "block echo x | gh pr create"', "one with a pipe"),
    ('git commit -m "gate\n\ngh pr create is refused"', "a multi-line one"),
    ("cat > t.py <<'EOF'\n    \"gh pr create\",\n    \"cd /x && gh pr create\",\nEOF",
     "writing THIS FILE, whose fixtures quote both commands and operators"),
    ("cat <<-EOF > d.md\n\tgh pr create\n\tEOF", "<<- lets the terminator be indented"),
    ("pipenv run python - <<'PY' 2>&1 | grep -v x\ns='gh pr create'\nPY\npipenv run pytest",
     "a heredoc whose redirection continues past the tag"),
])
def test_these_do_not(command, why):
    assert gate.opens_a_pr(command) is False, why


# --- the gate ---------------------------------------------------------------

def test_opening_a_pr_without_a_receipt_is_refused(repo):
    decision = run_hook("gh pr create --fill", repo)

    assert decision["permissionDecision"] == "deny"
    assert "readiness" in decision["permissionDecisionReason"].lower()


def test_a_complete_receipt_for_this_commit_opens_the_gate(repo):
    write_receipt(repo)

    assert run_hook("gh pr create --fill", repo) is None


def test_a_receipt_for_a_different_commit_does_not_count(repo):
    """The whole point of keying on the sha: a receipt earned before the last
    commit describes code that no longer exists."""
    write_receipt(repo, sha="0" * 40)

    decision = run_hook("gh pr create --fill", repo)

    assert decision["permissionDecision"] == "deny"
    assert "No PR-readiness receipt" in decision["permissionDecisionReason"]


def test_the_receipt_consulted_is_the_one_for_the_repo_being_worked_in(tmp_path, repo):
    """This project uses git worktrees, so the session's directory and the
    directory a command runs in are routinely different repos at different
    commits. Taking the session's HEAD would let a receipt for the main checkout
    unlock a PR for an unreviewed commit on a branch in a worktree."""
    other = tmp_path / "worktree"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=other, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=other, check=True)
    (other / "g").write_text("y")
    subprocess.run(["git", "add", "."], cwd=other, check=True)
    subprocess.run(["git", "commit", "-qm", "unreviewed"], cwd=other, check=True)
    write_receipt(repo)                      # the session's repo IS reviewed

    decision = run_hook("gh pr create --fill", repo, cwd=other)

    assert decision is not None, "a receipt for another repo unlocked this one"
    assert decision["permissionDecision"] == "deny"


@pytest.mark.parametrize("missing", PASSES)
def test_every_pass_is_required_individually(repo, missing):
    checks = {n: {"ok": True} for n in PASSES}
    checks[missing] = {"ok": False}
    write_receipt(repo, checks=checks)

    decision = run_hook("gh pr create --fill", repo)

    assert decision["permissionDecision"] == "deny"
    assert missing in decision["permissionDecisionReason"]


@pytest.mark.parametrize("value", ["no", 1, "false", {}, None])
def test_a_pass_must_be_exactly_true_not_merely_truthy(repo, value):
    """`{"ok": "no"}` is truthy. Checking truthiness rather than truth would let
    a malformed receipt -- or a hand-edited one -- open the gate."""
    checks = {n: {"ok": True} for n in PASSES}
    checks["pytest"] = {"ok": value}
    write_receipt(repo, checks=checks)

    assert run_hook("gh pr create --fill", repo) is not None


def test_a_named_override_ships_a_production_fix(repo):
    """An absolute gate has to be bypassed by editing it, and a gate people edit
    is worse than one that asks for a reason. The reason is typed on the command,
    so it lands in the transcript."""
    assert run_hook("gh pr create --fill", repo,
                    env_extra={"PR_READINESS_OVERRIDE": "prod down: NameError"}) is None


def test_an_empty_override_is_not_an_override(repo):
    """Otherwise an empty PR_READINESS_OVERRIDE left in a shell profile disables
    the gate permanently and invisibly."""
    decision = run_hook("gh pr create --fill", repo,
                        env_extra={"PR_READINESS_OVERRIDE": "   "})

    assert decision["permissionDecision"] == "deny"


def test_a_corrupt_receipt_is_refused_and_says_how_to_recover(repo):
    d = repo / ".claude" / "pr-readiness"
    d.mkdir(parents=True)
    (d / f"{head_of(repo)}.json").write_text("{not json")

    decision = run_hook("gh pr create --fill", repo)

    assert decision["permissionDecision"] == "deny"
    assert "unreadable" in decision["permissionDecisionReason"]
    assert "Delete" in decision["permissionDecisionReason"]


def test_malformed_hook_input_gets_no_opinion(repo):
    """Matches the other guardrail hooks: never be the reason a tool call dies."""
    proc = subprocess.run([sys.executable, str(HOOK)], input="not json",
                          capture_output=True, text=True,
                          env={"CLAUDE_PROJECT_DIR": str(repo), "PATH": "/usr/bin:/bin"})

    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# --- the recorder -----------------------------------------------------------

@pytest.mark.parametrize("output, code, ok", [
    ("1951 passed, 9 skipped, 21 warnings in 255.93s", 0, True),
    ("1 failed, 1950 passed, 9 skipped in 350.15s", 1, False),
    ("0 passed in 0.10s", 0, True),
    ("ERROR tests/x.py - ImportError\n!!! Interrupted: 1 error !!!", 2, False),
    ("", 1, False),
])
def test_the_pytest_parser_never_reads_a_failure_as_a_pass(output, code, ok):
    assert recorder._pytest_result(code, output)["ok"] is ok


@pytest.mark.parametrize("output, code, ok", [
    ("INFO 0 errors (3 suppressed, 8 warnings not shown)", 0, True),
    ("INFO 4 errors (3 suppressed)", 1, False),
    ("INFO 1 error", 1, False),
    ("some unexpected banner with no count", 0, False),
    ("INFO 0 errors", 1, False),
])
def test_the_pyrefly_parser_never_reads_a_failure_as_a_pass(output, code, ok):
    assert recorder._pyrefly_result(code, output)["ok"] is ok


def test_a_type_check_that_checked_nothing_is_not_a_pass():
    """From a git worktree, a bare pyrefly resolves the wrong interpreter,
    checks 0 files, and reports success (CLAUDE.md, "Type Checking"). Recording
    that as green is the one wrong answer available here."""
    result = recorder._pyrefly_result(0, "INFO 0 errors\n INFO checked 0 files")

    assert result["ok"] is False
    assert "0 files" in result["summary"]


def test_the_recorder_offers_a_flag_for_every_attested_pass():
    """The half-finished version of this refactor paired hook-owned NAMES with a
    hardcoded positional tuple: adding a pass would have left it with nowhere to
    be recorded, and the gate denying forever with no flag to fix it."""
    helptext = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "pr_readiness.py"), "--help"],
        capture_output=True, text=True, cwd=REPO).stdout

    for name in recorder.ATTESTED:
        assert f"--{name}" in helptext, f"no way to record {name}"
    assert set(recorder.MEASURED) | set(recorder.ATTESTED) == set(gate.REQUIRED)


def test_the_recorder_refuses_without_a_note_for_each_attested_pass():
    """The note is the only evidence these passes were real, so a receipt
    without one would record a checkbox."""
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "pr_readiness.py"), "--simplify", "x"],
        capture_output=True, text=True, cwd=REPO)

    assert proc.returncode != 0
    for name in ("review", "e2e"):
        assert f"--{name}" in proc.stdout + proc.stderr
