#!/usr/bin/env python3
"""PreToolUse hook: refuse to open or mark ready a PR that has not been through
the readiness passes.

Why each pass is required lives in one place -- "Before opening a PR" in
CLAUDE.md -- so it can be corrected once when it stops being true.

The failure this guards against is FORGETTING, not disagreeing. That shapes
every trade below: it must not miss an ordinary command typed with a stray
space, and it must not fire while someone writes a commit message about it.
It is not a sandbox, and a determined agent can route around it (a GraphQL
mutation, an MCP tool, a PR opened in the browser, a command inside a script
file this never reads). Guarding against evasion would need a different
mechanism -- a server-side check on the PR itself.

The receipt is bound to the exact commit: adding or amending a commit after the
passes ran invalidates it, because the thing that was reviewed no longer exists.
That binding has a cost worth knowing. Receipts are local files keyed by sha, so
rebasing a stack invalidates every one of them for passes genuinely done, and
a stack submit opens N PRs while only HEAD's receipt is checked. A
travelling artifact (a git note) would keep the binding and survive both; that is
the upgrade if this becomes a nuisance.

Escape hatch, for a production incident where the fix must ship now:

    PR_READINESS_OVERRIDE="prod is down: NameError in create_rooms_pairings" ...

which is allowed and recorded. It has to be typed, which is the point -- an
override is a decision someone made, not a step that quietly did not happen.
"""
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

# The gate's definition of "reviewed", and the only copy. scripts/pr_readiness.py
# imports these rather than listing them again: two lists that drifted would have
# the gate demanding a pass nothing records, denying forever and reading as a bug
# in the hook rather than a one-word mismatch.
MEASURED = ("pytest", "pyrefly")           # the recorder runs these itself
ATTESTED = ("simplify", "review", "e2e")   # these need judgement and a written note
REQUIRED = MEASURED + ATTESTED

# Subcommands that put work in front of a reviewer. `git push` is deliberately
# absent: pushing a branch to back it up or share a WIP makes no such claim, and
# neither does reading or merging a PR a human already approved.
GATED_SUBCOMMANDS = (("pr", "create"), ("pr", "ready"), ("stack", "submit"))

# Things that sit in front of a command without changing which command it is.
WRAPPERS = {"command", "builtin", "exec", "env", "nohup", "time", "sudo", "xargs",
            "stdbuf", "nice", "setsid"}
# Shell keywords that can precede a command inside a compound statement.
KEYWORDS = {"then", "else", "elif", "do", "!", "{", "}", "(", ")"}
SHELLS = {"sh", "bash", "zsh", "dash"}
ASSIGNMENT = re.compile(r"^\w+=")
# Operators that end one simple command and begin the next -- but only outside
# quotes. Splitting the raw string instead would tear `git commit -m "a (b) c"`
# into pieces and read the fragment as a command, which is the false positive
# this whole approach exists to avoid.
OPERATORS = (";", "|", "&", "\n", "(", ")", "`")


def _strip_heredocs(text: str) -> str:
    """Remove heredoc bodies: what is being WRITTEN is not what is being run.

    Only an unquoted `<<TAG` opens one. Checking that matters -- `grep -n
    "<<EOF" doc.md` mentions a tag inside a string, and treating it as a real
    redirection swallows every following line up to the next `EOF`, which is a
    way to hide a real command from this hook.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        tag = _heredoc_tag(line)
        i += 1
        if tag is None:
            continue
        # `<<-` allows the terminator to be indented with tabs.
        dash, name = tag
        while i < len(lines):
            candidate = lines[i].lstrip("\t") if dash else lines[i]
            i += 1
            if candidate.strip() == name:
                break
    return "\n".join(out)


def _heredoc_tag(line: str):
    """(is_dash_form, tag) if this line opens a heredoc outside quotes."""
    quote = None
    j = 0
    while j < len(line):
        ch = line[j]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"':
                j += 1
        elif ch in "'\"":
            quote = ch
        elif ch == "<" and line[j + 1:j + 2] == "<":
            rest = line[j + 2:]
            dash = rest.startswith("-")
            rest = rest[1:] if dash else rest
            m = re.match(r"\s*['\"]?(\w+)['\"]?", rest)
            if m and not rest.lstrip().startswith("<"):   # not `<<<` here-string
                return dash, m.group(1)
        j += 1
    return None


def _commands(text: str) -> list[list[str]]:
    """Every simple command in `text`, as argv lists.

    Tokenised rather than pattern-matched on the raw string. That is what makes
    `  gh pr create` (a stray space) the same as `gh pr create`, while
    `git commit -m "gh pr create"` is not -- the quoted mention collapses into a
    single argument of git, which is exactly the distinction being drawn.
    """
    text = text.replace("\\\n", " ")          # join line continuations
    found: list[list[str]] = []
    for segment in _split_unquoted(_strip_heredocs(text)):
        if not segment.strip():
            continue
        try:
            argv = shlex.split(segment, comments=True)
        except ValueError:
            continue                          # unbalanced quotes: no opinion
        found.extend(_peel(argv))
    return found


def _split_unquoted(text: str) -> list[str]:
    """Break `text` at shell operators that are not inside quotes.

    A single-quoted stretch is inert, so nothing splits inside it. Backticks and
    `$(` open a command substitution, whose contents ARE run -- so they separate
    commands rather than hiding them.
    """
    parts: list[str] = []
    current: list[str] = []
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\\" and quote == '"':
                current.append(ch)
                i += 1
                if i < len(text):
                    current.append(text[i])
                i += 1
                continue
            elif ch == "`" and quote == '"':
                parts.append("".join(current))
                current = []
                i += 1
                continue
            current.append(ch)
        elif ch in "'\"":
            quote = ch
            current.append(ch)
        elif text[i:i + 2] == "$(":
            parts.append("".join(current))
            current = []
            i += 2
            continue
        elif ch in OPERATORS:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def _peel(argv: list[str]) -> list[list[str]]:
    """Drop assignments, keywords and wrappers; follow `bash -c` into its script."""
    while argv and (ASSIGNMENT.match(argv[0]) or argv[0] in KEYWORDS
                    or argv[0] in WRAPPERS):
        argv = argv[1:]
    if not argv:
        return []
    name = os.path.basename(argv[0].lstrip("\\"))
    if name in SHELLS and "-c" in argv:
        script = argv[argv.index("-c") + 1:argv.index("-c") + 2]
        return _commands(script[0]) if script else []
    return [[name] + argv[1:]]


def opens_a_pr(command: str) -> bool:
    """Does this command line create a PR or mark one ready?"""
    for argv in _commands(command):
        if argv[0] != "gh":
            continue
        rest = argv[1:]
        if any(tuple(rest[:len(sub)]) == sub for sub in GATED_SUBCOMMANDS):
            return True
        # The REST spelling of the same act. `gh api repos/o/r/pulls -f head=...`
        # opens a PR knowing nothing about `gh pr create`; gh switches to POST as
        # soon as a field is supplied, so any field flag counts as a write.
        if rest[:1] == ["api"] and any("pulls" in a for a in rest):
            writes = {"-X", "--method", "-f", "--field", "-F", "--raw-field", "--input"}
            if any(a in writes or a.split("=")[0] in writes for a in rest):
                return True
    return False


def deny(reason: str) -> NoReturn:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        command = payload.get("tool_input", {}).get("command") or ""
    except Exception:
        return  # malformed input: no opinion, same as the other guardrail hooks

    if not opens_a_pr(command):
        return

    if os.environ.get("PR_READINESS_OVERRIDE", "").strip():
        return  # a deliberate, typed choice; it lands in the transcript

    # The repo the command will run in, not the one the session started in.
    # This project uses git worktrees, where those differ -- and taking the
    # session's HEAD would let a receipt for the main checkout unlock a PR for
    # an unreviewed commit on a branch in a worktree.
    root = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or "."
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                              capture_output=True, text=True, check=True).stdout.strip()
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                             capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return  # not a git repo, or git unavailable: not this hook's business

    receipt = Path(top) / ".claude" / "pr-readiness" / f"{head}.json"
    how = ("Run:  pipenv run python scripts/pr_readiness.py --help\n"
           "It runs the suite and the type checker itself, and takes your notes "
           "for the passes it cannot run for you.")
    hatch = ('If this is a production incident, set PR_READINESS_OVERRIDE="<why>" '
             "on the command and it will be allowed.")

    if not receipt.exists():
        deny(f"No PR-readiness receipt for HEAD ({head[:8]}).\n\n"
             f"Required: {', '.join(REQUIRED)}. See \"Before opening a PR\" in "
             f"CLAUDE.md for why each one is there.\n\n{how}\n\n{hatch}")

    try:
        data = json.loads(receipt.read_text())
    except Exception as e:
        deny(f"The readiness receipt for {head[:8]} is unreadable ({e}).\n"
             f"Delete {receipt} and record it again.\n\n{how}")

    checks = data.get("checks", {})
    missing = [name for name in REQUIRED
               if not isinstance(checks.get(name), dict) or checks[name].get("ok") is not True]
    if missing:
        deny(f"The readiness receipt for {head[:8]} is incomplete.\n\n"
             f"Not passing: {', '.join(missing)}\n\n{how}\n\n{hatch}")


if __name__ == "__main__":
    main()
