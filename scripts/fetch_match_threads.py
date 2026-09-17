#!/usr/bin/env python3
"""Read-only dump of the pairing threads for tournament matches with no result.

Answers "what are these teams actually saying?" for matches the bot still shows
as unplayed. Read-only: it GETs messages and writes a local file. Nothing is
posted, edited or deleted.

Run on the box where the PRODUCTION bot token lives (the droplet), same as
scripts/fetch_league_chat.py. It uses only the Discord REST API -- no gateway
connection -- so the running bot is undisturbed.

Usage:
    python3 scripts/fetch_match_threads.py [TOURNAMENT_ID]     # default 3

Picks up every match in the tournament whose result is still NULL, reads its
thread, and prints a per-thread summary: who spoke, when they last spoke, and
the messages themselves.

Token resolution (first found wins):
    - env var BOT_TOKEN
    - a BOT_TOKEN=... line in ./.env

Output:
    stdout summary, plus match_threads_<TOURNAMENT_ID>.json in the cwd
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://discord.com/api/v10"
DB = "drafts.db"
PER_THREAD = 100          # newest N messages per thread; plenty for scheduling chat


def load_token():
    token = os.environ.get("BOT_TOKEN")
    if token:
        return token.strip()
    try:
        with open(".env") as fh:
            for line in fh:
                if line.startswith("BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    sys.exit("BOT_TOKEN not found in environment or ./.env")


def api_get(path, token, params=None):
    """GET an API path, retrying on 429. None on 403/404 (gone or no access)."""
    url = f"{API}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bot {token}",
        "User-Agent": "DraftBot-thread-export/1.0",
    })
    while True:
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry = 1.0
                try:
                    retry = float(json.loads(e.read().decode()).get("retry_after", 1.0))
                except Exception:
                    pass
                time.sleep(retry + 0.5)
                continue
            if e.code in (403, 404):
                return None
            raise


def outstanding(tournament_id):
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT m.id, r.round_number, pa.team_name, pb.team_name, m.thread_id,
               (SELECT COUNT(*) FROM draft_sessions ds WHERE ds.tournament_match_id = m.id)
        FROM tournament_matches m
        JOIN tournament_rounds r ON r.id = m.round_id
        LEFT JOIN tournament_participants pa ON pa.id = m.team_a_participant_id
        LEFT JOIN tournament_participants pb ON pb.id = m.team_b_participant_id
        WHERE r.tournament_id = ? AND m.team_a_wins IS NULL AND m.is_bye = 0
        ORDER BY m.id""", (tournament_id,)).fetchall()
    con.close()
    return rows


def age(stamp):
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except Exception:
        return "?"
    hrs = (datetime.now(timezone.utc) - then).total_seconds() / 3600
    return f"{hrs:.0f}h ago" if hrs < 48 else f"{hrs/24:.1f}d ago"


def main():
    tid = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    token = load_token()
    rows = outstanding(tid)
    if not rows:
        print(f"tournament {tid}: no matches outstanding")
        return

    dump = {}
    for mid, rnd, team_a, team_b, thread_id, drafts in rows:
        head = f"match {mid} (round {rnd})  {team_a}  vs  {team_b}"
        print("\n" + "=" * len(head))
        print(head)
        print(f"draft sessions linked: {drafts}")
        if not thread_id:
            print("  no thread recorded")
            continue

        msgs = api_get(f"/channels/{thread_id}/messages", token,
                       {"limit": PER_THREAD})
        if msgs is None:
            print(f"  thread {thread_id}: no access or deleted")
            continue
        msgs = list(reversed(msgs))          # oldest first
        dump[str(mid)] = {
            "round": rnd, "team_a": team_a, "team_b": team_b,
            "thread_id": thread_id,
            "messages": [{
                "id": m["id"],
                "author": (m.get("author") or {}).get("username"),
                "bot": bool((m.get("author") or {}).get("bot")),
                "at": m.get("timestamp"),
                "content": m.get("content"),
                "embeds": len(m.get("embeds") or []),
            } for m in msgs],
        }

        human = [m for m in msgs if not (m.get("author") or {}).get("bot")]
        print(f"  {len(msgs)} messages, {len(human)} from people")
        if human:
            last = human[-1]
            print(f"  last human message: {age(last.get('timestamp'))} "
                  f"by {(last.get('author') or {}).get('username')}")
        else:
            print("  nobody has said anything")
        for m in human:
            who = (m.get("author") or {}).get("username")
            body = " ".join((m.get("content") or "").split())
            if not body:
                continue
            print(f"    [{(m.get('timestamp') or '')[:16]}] {who}: {body[:220]}")

    out = f"match_threads_{tid}.json"
    with open(out, "w") as fh:
        json.dump(dump, fh, indent=1)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
