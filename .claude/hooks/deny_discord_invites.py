#!/usr/bin/env python3
"""PreToolUse hook: deny in-app browser navigation to Discord invite /
guild-creation URLs, so the discord-test harness can never join or create
guilds regardless of model behavior. Fails open on malformed input (no
opinion) and stays silent for every other URL."""
import json
import re
import sys

BLOCKED = re.compile(
    r"(^|[./])(discord\.gg|discord\.new)(/|$)|discord(app)?\.com/invite",
    re.IGNORECASE,
)


def main():
    try:
        payload = json.load(sys.stdin)
        url = payload.get("tool_input", {}).get("url") or ""
    except Exception:
        return  # malformed input: no opinion

    if BLOCKED.search(url):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Blocked by the discord-test harness guardrails: Discord "
                    "invite/guild-creation URLs are never navigated (no "
                    "joining or creating guilds)."
                ),
            }
        }))


if __name__ == "__main__":
    main()
