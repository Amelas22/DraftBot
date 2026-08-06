#!/usr/bin/env python3
"""PreToolUse hook: confine in-app browser navigation of Discord CHANNEL urls
to the guild named by TEST_GUILD_ID in the repo's .env — the mechanical
enforcement of the discord-test skill's "act only in the test guild" rail.

Scope is deliberately narrow so non-skill sessions are unaffected: only
`discord.com/channels/<something>` urls are judged. Those fail CLOSED when the
guild can't be validated (unknown guild id, `@me` DMs, or TEST_GUILD_ID
missing); every other url — including discord.com/login for the setup
handoff — passes without opinion. Malformed hook input: no opinion (matching
deny_discord_invites.py)."""
import json
import os
import re
import sys

CHANNELS = re.compile(
    r"(?:^|//|\.)(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/([^/?#]+)",
    re.IGNORECASE,
)


def configured_test_guild():
    env_path = os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", "."), ".env")
    try:
        with open(env_path) as f:
            for line in f:
                m = re.match(r"\s*TEST_GUILD_ID\s*=\s*['\"]?(\d+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def main():
    try:
        payload = json.load(sys.stdin)
        url = payload.get("tool_input", {}).get("url") or ""
    except Exception:
        return  # malformed input: no opinion

    m = CHANNELS.search(url)
    if not m:
        return  # not a Discord channel url: no opinion

    guild = m.group(1)
    allowed = configured_test_guild()
    if allowed is None:
        deny(
            "Blocked by the discord-test harness guardrails: TEST_GUILD_ID is "
            "not set in .env, so the target guild cannot be verified. Set it "
            "before navigating Discord channels."
        )
    elif guild != allowed:
        deny(
            f"Blocked by the discord-test harness guardrails: guild '{guild}' "
            "is not the configured test guild (TEST_GUILD_ID). Navigation is "
            "confined to the test guild; DMs (@me) are never navigated."
        )


if __name__ == "__main__":
    main()
