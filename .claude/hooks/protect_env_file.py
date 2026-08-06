#!/usr/bin/env python3
"""PreToolUse hook: deny Bash commands that WRITE to any dotenv-family file —
.env and its aliases (.env.local, .env-test, .env.backup, ...) — via
redirection, in-place edits, overwrites, or deletion. Companion to the
permissions.deny rules that block the Edit/Write tools on .env*; this closes
the shell path.

Reads stay allowed: the harness legitimately greps .env for key presence
(prefer targeted greps over dumping the file; BOT_TOKEN lives there).
Malformed input: no opinion (matching the other guardrail hooks)."""
import json
import re
import sys

# The dotenv FAMILY: .env plus separator-joined aliases (.env.local,
# .env-test, .env_prod...) and reverse-style names (backup.env, prod.env).
# The lookahead excludes unrelated names like .environment_notes.txt (no
# separator after "env").
ENV = r"(?:[\w./~$-]*/)?[\w.-]*\.env(?:[._-][\w.-]*)?(?![\w-])"
WRITE_PATTERNS = re.compile("|".join([
    rf"(?:>>?\s*{ENV})",                     # > .env / >> .env
    rf"(?:\bsed\b[^|;&]*-i[^|;&]*{ENV})",    # sed -i ... .env
    rf"(?:\btee\b[^|;&]*{ENV})",             # tee [-a] .env
    rf"(?:\b(?:rm|truncate|unlink)\b[^|;&]*{ENV})",
    rf"(?:\b(?:mv|cp)\b[^|;&]+\s{ENV}\s*(?:$|[|;&]))",  # .env as DEST (last arg)
]), re.IGNORECASE)


def main():
    try:
        payload = json.load(sys.stdin)
        command = payload.get("tool_input", {}).get("command") or ""
    except Exception:
        return  # malformed input: no opinion

    if WRITE_PATTERNS.search(command):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Blocked by the discord-test harness guardrails: .env is "
                    "developer-owned (it holds BOT_TOKEN and the test-guild "
                    "settings) — Claude never writes it. Ask the developer to "
                    "make the change."
                ),
            }
        }))


if __name__ == "__main__":
    main()
