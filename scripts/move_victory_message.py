"""Move one draft's victory post from one results channel to another.

The results channel follows `tournament_match_id` (utils.results_channel_name_for):
a draft that records into a tournament posts to #league-draft-results, everything
else to #team-draft-results. A post can still end up in the wrong one — a draft
linked or unlinked after it had already posted, or anything routed under the
older rule, which read `session_type` and sent every premade draft to the league
channel whether or not it was a league match. This script corrects one by hand.

It re-posts the identical embeds in the target channel, deletes the original,
and repoints `draft_sessions.victory_message_id_results_channel` at the new
message, in that order: a failure after the post leaves a duplicate (visible,
harmless, fixable), while a failure after the delete would lose the post.

NOT self-healing. The stored id carries no channel, so the victory path
re-derives the channel on every re-render, fails to find the moved id there,
and posts a fresh copy back into the channel the rule picks. Any later
re-render (an outstanding match result, the deferred logs-link update) undoes
this move. Check the draft has nothing left to report before running.

    pipenv run python scripts/move_victory_message.py --friendly-id river-kelpie-42 --to team-draft-results
    pipenv run python scripts/move_victory_message.py --friendly-id river-kelpie-42 --to team-draft-results --apply

BOT_TOKEN is read from env or ./.env. --apply must run where the bot's
drafts.db is (the droplet); --preview reads Discord but never writes.
"""
import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request

API = "https://discord.com/api/v10"


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
    return None


def _discord(method, path, token, body=None):
    """One Discord REST call, retrying on 429. Returns parsed JSON (or {})."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Authorization": f"Bot {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "DraftBot-victory-move/1.0"},
    )
    while True:
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry = 1.0
                try:
                    retry = float(json.loads(e.read().decode()).get("retry_after", 1.0))
                except Exception:
                    pass
                time.sleep(retry + 0.5)
                continue
            raise


def load_draft(db_path, friendly_id, session_id):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    if friendly_id:
        where, param = "friendly_id = ?", friendly_id
    else:
        where, param = "session_id = ?", session_id
    row = con.execute(
        f"SELECT session_id, friendly_id, guild_id, session_type, session_stage,"
        f" victory_message_id_results_channel FROM draft_sessions WHERE {where}",
        (param,)).fetchone()
    con.close()
    if row is None:
        raise SystemExit(f"No draft matching {param!r} in {db_path}.")
    if not row["victory_message_id_results_channel"]:
        raise SystemExit(
            f"{row['friendly_id']} has no victory_message_id_results_channel — "
            "nothing was posted to a results channel, so there is nothing to move.")
    return dict(row)


def locate_message(token, guild_id, message_id):
    """The text channel that actually holds `message_id`, plus the guild's channels.

    Asking Discord beats re-deriving the channel from the routing rule: the
    post may predate the current rule, and duplicate channel names are a known
    hazard here (find_postable_results_channel exists precisely because a stale
    #league-draft-results outlived its replacement). The channel holding the
    message is the only one that matters.
    """
    channels = [c for c in _discord("GET", f"/guilds/{guild_id}/channels", token)
                if c.get("type") == 0]
    for ch in channels:
        try:
            msg = _discord("GET", f"/channels/{ch['id']}/messages/{message_id}", token)
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                continue
            raise
        return ch, msg, channels
    raise SystemExit(
        f"Message {message_id} is not in any text channel the bot can read in "
        f"guild {guild_id}. It may already have been deleted or moved.")


def resolve_target(channels, name):
    matches = [c for c in channels if c["name"] == name]
    if not matches:
        raise SystemExit(f"No text channel named #{name} in this guild.")
    if len(matches) > 1:
        ids = ", ".join(c["id"] for c in matches)
        raise SystemExit(
            f"{len(matches)} channels are named #{name} ({ids}). Refusing to guess "
            "— delete or rename the stale one first.")
    return matches[0]


def describe(msg):
    out = []
    for e in msg.get("embeds", []):
        out.append(f"      title:  {e.get('title', '(none)')}")
        fields = ", ".join(f.get("name", "?") for f in e.get("fields", []))
        out.append(f"      fields: {fields or '(none)'}")
    return "\n".join(out) or "      (no embeds)"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ident = ap.add_mutually_exclusive_group(required=True)
    ident.add_argument("--friendly-id", help="e.g. river-kelpie-42")
    ident.add_argument("--session-id")
    ap.add_argument("--to", required=True, help="target channel name, e.g. team-draft-results")
    ap.add_argument("--db", default="drafts.db")
    ap.add_argument("--apply", action="store_true", help="write; default is a read-only preview")
    ap.add_argument("--force", action="store_true",
                    help="move even if the message carries components or attachments "
                         "(they cannot be recreated and will be lost)")
    args = ap.parse_args()

    token = load_token()
    if not token:
        raise SystemExit("BOT_TOKEN not found in the environment or ./.env")

    draft = load_draft(args.db, args.friendly_id, args.session_id)
    message_id = draft["victory_message_id_results_channel"]
    source, msg, channels = locate_message(token, draft["guild_id"], message_id)
    target = resolve_target(channels, args.to)

    print(f"draft    {draft['friendly_id']}  ({draft['session_id']})")
    print(f"         session_type={draft['session_type']}  stage={draft['session_stage']}")
    print(f"message  {message_id}")
    print(f"from     #{source['name']}  ({source['id']})")
    print(f"to       #{target['name']}  ({target['id']})")
    print("embeds:")
    print(describe(msg))

    if source["id"] == target["id"]:
        raise SystemExit("\nAlready in the target channel — nothing to do.")

    lossy = []
    if msg.get("components"):
        lossy.append("components (buttons)")
    if msg.get("attachments"):
        lossy.append("attachments")
    if lossy and not args.force:
        raise SystemExit(
            f"\nMessage carries {' and '.join(lossy)}, which a re-post cannot "
            "recreate. Re-run with --force to move it anyway and lose them.")

    if not args.apply:
        print("\n(preview — nothing written; re-run with --apply)")
        return

    body = {"embeds": msg.get("embeds", [])}
    if msg.get("content"):
        body["content"] = msg["content"]
    posted = _discord("POST", f"/channels/{target['id']}/messages", token, body)
    new_id = posted["id"]
    print(f"\nposted   {new_id} in #{target['name']}")

    _discord("DELETE", f"/channels/{source['id']}/messages/{message_id}", token)
    print(f"deleted  {message_id} from #{source['name']}")

    con = sqlite3.connect(args.db)
    with con:
        con.execute("UPDATE draft_sessions SET victory_message_id_results_channel = ? "
                    "WHERE session_id = ?", (str(new_id), draft["session_id"]))
    con.close()
    print(f"repointed draft_sessions.victory_message_id_results_channel -> {new_id}")
    print(f"\nReminder: a re-render of {draft['friendly_id']} will post a fresh copy "
          f"back into #{source['name']}. Nothing in the DB prevents that.")


if __name__ == "__main__":
    main()
