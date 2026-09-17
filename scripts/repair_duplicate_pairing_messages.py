"""One-off: repair the duplicate pairing messages left by a double pairing run.

Companion to repair_duplicate_pairing_rows.py, which repairs the database. This
repairs what players actually see in the draft chat. Run it AFTER the row
repair, because it decides what to keep by comparing against the rows.

When create_rooms_pairings ran twice for one session, post_pairings ran twice
too. ensure_channel made the second run REUSE the first run's rooms, so there
are no orphan channels -- but post_pairings sends unconditionally, so the same
channel received two full sets of "Round N Pairings" messages.

The two sets are NOT equivalent, and which one is tracked is the opposite of
what you would guess. The first run rendered while only its own rows existed,
so its messages are correct: one field and one button per match. The second run
rendered after both runs had committed, so its messages list every match twice.
post_pairings then overwrote match_results.pairing_message_id with the second
run's ids -- so the database tracks the mangled set and the clean set is
orphaned.

That mistracking is what players report as buttons that do not update.
update_pairings_posting repaints strictly by pairing_message_id, so a click on
the clean message records the result and then repaints a different message.

So the rule is not "keep the tracked set". It is: keep the set that MATCHES THE
REPAIRED DATABASE -- one embed field per row in that round -- delete the other,
and repoint pairing_message_id at what survives. The result is a draft chat
with exactly one correct pairings message per round, tracked, whose buttons the
running bot already owns.

    pipenv run python scripts/repair_duplicate_pairing_messages.py --session <id>
    pipenv run python scripts/repair_duplicate_pairing_messages.py --session <id> --apply

Dry run by default, and the dry run is pure reads. Must run where the bot's
drafts.db and BOT_TOKEN are (the droplet).

It never edits a message and never posts one. create_pairings_view builds
buttons with no custom_id, so a view written by any process other than the
running bot carries ids that bot has never registered -- posting a "corrected"
message would replace working buttons with dead ones. Deleting a message and
repointing a row are the only safe moves from outside.

It refuses a round where no surviving message has the expected field count, or
where more than one does, rather than guessing which set is canonical.
"""
import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from models.draft_session import DraftSession  # noqa: E402
from models.match import MatchResult  # noqa: E402

load_dotenv()

API = "https://discord.com/api/v10"
PAIRINGS_TITLE = re.compile(r"^Round (\d+) Pairings$")
REMINDER_PREFIX = "**REMINDER**"
DEFAULT_DB = "sqlite+aiosqlite:///drafts.db"
MAX_PAGES = 20  # 2000 messages; a draft chat never buries its pairings deeper


async def _request(method, http, url, **kw):
    async with http.request(method, url, **kw) as resp:
        if resp.status == 429:
            retry = (await resp.json()).get("retry_after", 1)
            await asyncio.sleep(float(retry) + 0.5)
            return await _request(method, http, url, **kw)
        if method == "DELETE":
            if resp.status not in (204, 404):
                raise RuntimeError(f"delete failed: {resp.status} {await resp.text()}")
            return resp.status
        resp.raise_for_status()
        return await resp.json()


async def get_json(http, url):
    return await _request("GET", http, url)


async def delete_message(http, channel_id, message_id):
    return await _request("DELETE", http, f"{API}/channels/{channel_id}/messages/{message_id}")


async def fetch_history(http, channel_id, needed_ids):
    """Messages newest-first, paging back until every needed id is seen.

    The first attempt at this repair refused a busy channel because it only
    looked at the last 100 messages. Stopping as soon as the tracked ids are
    all accounted for keeps a quiet channel to a single request.
    """
    out, before = [], None
    for _ in range(MAX_PAGES):
        url = f"{API}/channels/{channel_id}/messages?limit=100"
        if before:
            url += f"&before={before}"
        page = await get_json(http, url)
        if not page:
            break
        out.extend(page)
        before = page[-1]["id"]
        if needed_ids.issubset({m["id"] for m in out}):
            break
    return out


def round_of(match_number, team_size):
    """Mirrors post_pairings' grouping exactly."""
    return (match_number - 1) // (team_size or 1) + 1


async def repair_session(http, db, session_id, apply):
    draft = await db.scalar(select(DraftSession).filter_by(session_id=session_id))
    if not draft:
        print(f"{session_id}: draft session not found")
        return 1
    if not draft.draft_chat_channel:
        print(f"{session_id}: no draft_chat_channel recorded")
        return 1

    channel_id = int(draft.draft_chat_channel)
    rows = (await db.scalars(
        select(MatchResult).where(MatchResult.session_id == session_id))).all()
    if not rows:
        print(f"{session_id}: no match results")
        return 1

    team_size = len(draft.team_a or [])
    expected = {}
    for row in rows:
        expected.setdefault(round_of(row.match_number, team_size), []).append(row)
    tracked = {str(r.pairing_message_id) for r in rows if r.pairing_message_id}

    print(f"\n{session_id}  channel {channel_id}")
    for rnd in sorted(expected):
        print(f"  round {rnd}: {len(expected[rnd])} match rows in the database")

    messages = await fetch_history(http, channel_id, tracked)
    print(f"  scanned {len(messages)} messages")

    by_round = {}
    reminders = []
    for m in messages:
        embeds = m.get("embeds") or []
        title = embeds[0].get("title") or "" if embeds else ""
        hit = PAIRINGS_TITLE.match(title)
        if hit:
            by_round.setdefault(int(hit.group(1)), []).append(m)
        elif (m.get("content") or "").startswith(REMINDER_PREFIX):
            reminders.append(m)

    keep, doomed, refused = {}, [], []
    for rnd, wanted in sorted(expected.items()):
        candidates = by_round.get(rnd, [])
        if not candidates:
            refused.append(f"round {rnd}: no pairings message found in the channel")
            continue
        matching = [m for m in candidates
                    if len((m["embeds"][0].get("fields") or [])) == len(wanted)]
        if len(matching) != 1:
            refused.append(
                f"round {rnd}: {len(matching)} of {len(candidates)} messages have the "
                f"expected {len(wanted)} fields -- cannot pick a canonical one")
            continue
        chosen = matching[0]
        keep[rnd] = chosen
        doomed.extend(m for m in candidates if m["id"] != chosen["id"])

    for rnd in sorted(by_round):
        for m in by_round[rnd]:
            fields = len(m["embeds"][0].get("fields") or [])
            buttons = sum(len(r.get("components") or []) for r in (m.get("components") or []))
            state = "KEEP  " if keep.get(rnd, {}).get("id") == m["id"] else "DELETE"
            note = " (currently tracked)" if m["id"] in tracked else ""
            print(f"  {state} round {rnd}  {m['id']}  {fields} fields, {buttons} buttons{note}")

    if refused:
        for line in refused:
            print(f"  REFUSED {line}")
        print("  nothing deleted, nothing repointed")
        return 1

    # Each post_pairings run sent its own reminder. Keep the first one that sits
    # BELOW the surviving pairings so the channel still reads in order, falling
    # back to the oldest if every reminder precedes them. Purely cosmetic --
    # the reminders carry no buttons and nothing references them.
    doomed_reminders = []
    if len(reminders) > 1:
        newest_kept = max(int(m["id"]) for m in keep.values())
        below = [m for m in reminders if int(m["id"]) > newest_kept]
        survivor = min(below, key=lambda m: int(m["id"])) if below else \
            min(reminders, key=lambda m: int(m["id"]))
        doomed_reminders = [m for m in reminders if m["id"] != survivor["id"]]
    for m in doomed_reminders:
        print(f"  DELETE {m['id']}  (duplicate REMINDER)")

    repoint = []
    for rnd, message in keep.items():
        for row in expected[rnd]:
            if str(row.pairing_message_id) != message["id"]:
                repoint.append((row, message["id"]))
    for row, new_id in repoint:
        print(f"  REPOINT match {row.match_number}: {row.pairing_message_id} -> {new_id}")

    if not doomed and not doomed_reminders and not repoint:
        print("  already clean")
        return 0

    if not apply:
        print(f"  dry run -- would delete {len(doomed) + len(doomed_reminders)} message(s) "
              f"and repoint {len(repoint)} row(s)")
        return 0

    # Repoint BEFORE deleting: if the delete half fails, the database already
    # tracks a message that exists, which is the state the bot can work with.
    for row, new_id in repoint:
        row.pairing_message_id = new_id
        db.add(row)
    await db.commit()
    print(f"  repointed {len(repoint)} row(s)")

    for m in doomed + doomed_reminders:
        status = await delete_message(http, channel_id, m["id"])
        print(f"  deleted {m['id']} ({status})")
        await asyncio.sleep(0.4)

    after = await fetch_history(http, channel_id, {m["id"] for m in keep.values()})
    left = {}
    for m in after:
        embeds = m.get("embeds") or []
        hit = PAIRINGS_TITLE.match(embeds[0].get("title") or "" if embeds else "")
        if hit:
            left.setdefault(int(hit.group(1)), []).append(m["id"])
    bad = {r: ids for r, ids in left.items() if len(ids) != 1 or ids[0] != keep[r]["id"]}
    if bad or set(left) != set(keep):
        print(f"  VERIFY FAILED: pairing messages now {left}")
        return 1
    print(f"  verified: exactly one correct pairings message per round, all tracked")
    return 0


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="append", dest="sessions", required=True,
                        help="session_id to repair (repeatable)")
    parser.add_argument("--apply", action="store_true", help="actually change things")
    parser.add_argument("--database-url", default=DEFAULT_DB)
    args = parser.parse_args()

    token = os.getenv("BOT_TOKEN")
    if not token:
        print("BOT_TOKEN is not set -- run this on the droplet")
        return 1

    engine = create_async_engine(args.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    rc = 0
    async with aiohttp.ClientSession(headers={"Authorization": f"Bot {token}"}) as http:
        async with Session() as db:
            for session_id in args.sessions:
                rc |= await repair_session(http, db, session_id, args.apply)
    await engine.dispose()
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
