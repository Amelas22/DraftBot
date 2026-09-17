#!/usr/bin/env python3
"""Undo the drafts_participated a failed create_rooms_pairings counted twice.

create_rooms_pairings does its once-only work -- pairings, player stats -- before
it creates any channel, and update_player_stats_for_draft commits in its OWN
session. So when the run crashes after that point, the stats stick while
everything in the outer transaction rolls back.

The re-run guard exists for exactly this:

    resuming = session.draft_chat_channel is not None

but it reads a channel created AFTER the stats, so a crash upstream of channel
creation leaves it False forever. Every press of the button then adds another
drafts_participated to all eight players.

That is what the SHARED_CHAT_TEAM NameError did on 2026-09-04: nine attempts on
session 532965473822965781-1788525115, nine increments each, zero drafts played.

Only PlayerStats is affected. PlayerWeeklyLimit.drafts_participated is written
by check_and_post_victory_or_draw, which runs when a draft FINISHES, so no
signup limit was touched.

Usage (dry run prints the table and writes nothing):

    pipenv run python scripts/repair_retry_inflated_draft_counts.py \
        --session-id 532965473822965781-1788525115 --from-journal
    pipenv run python scripts/repair_retry_inflated_draft_counts.py \
        --session-id 532965473822965781-1788525115 --from-journal --commit

Run it AFTER the fix is deployed. --from-journal counts the increments the bot
actually logged and subtracts the one legitimate run, so it stays correct however
many times the button was pressed -- prefer it to passing --spurious by hand.

No sudo: the droplet's service journal reads fine without it, and a sudo that
prompts over ssh hangs instead of failing.
"""
import argparse
import asyncio
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

sys.path.append(str(Path(__file__).parent.parent))

from session import AsyncSessionLocal
from models.draft_session import DraftSession
from models.player import PlayerStats

JOURNAL_MARKER = "Updating player stats for session_id={}"

# A relative adjustment is not safe to repeat: run it twice and the second run
# subtracts a correction that has already been made. There is no column that
# records the repair, so it is recorded here, next to the database it applied
# to, and a second run refuses rather than silently halving eight players.
RECEIPTS = Path(__file__).parent / ".repairs"


def receipt_for(session_id: str) -> Path:
    return RECEIPTS / f"draft-counts-{session_id}.done"


def count_stat_runs_in_journal(session_id: str, since: str) -> int:
    """How many times the bot logged the stats update for this session.

    Counted from the journal rather than from any database column because
    nothing in the schema records an attempt that rolled back -- the log line is
    the only trace a failed run leaves.
    """
    # Streamed, never accumulated. The droplet has ~2GB and the bot is using
    # half of it; reading a week of DEBUG-level journal into one string reached
    # 834MB RSS and got this script OOM-killed. The OOM killer picked the
    # script that time -- it could as easily have picked the bot.
    #
    # Do NOT wrap this in sudo. On the droplet the service journal is readable
    # without it, and a sudo that decides to prompt has nowhere to prompt from
    # over ssh -- it just hangs until something kills it.
    marker = JOURNAL_MARKER.format(session_id)
    hits = 0
    proc = subprocess.Popen(
        ["journalctl", "-u", "draftbot.service", "--no-pager", "-o", "cat",
         "--since", since],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    assert proc.stdout is not None
    with proc.stdout as stream:
        for line in stream:
            if marker in line:
                hits += 1
    if proc.wait() != 0:
        raise SystemExit("journalctl failed; check the unit name and permissions.")

    if hits == 0:
        # Without privileges journalctl prints nothing and still exits 0, which
        # would read as "no spurious runs" and quietly do nothing -- the one
        # wrong answer this script must never give. A session that reached the
        # stats update logged it at least once, so zero means the journal was
        # not readable (run under sudo) or has rotated (pass --spurious).
        raise SystemExit(
            "journalctl returned no matching lines for this session.\n"
            "  Widen --journal-since (the default only looks back 7 days), or --\n"
            "  if the journal has rotated past the attempts -- pass --spurious N.")
    return hits


async def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session-id", required=True)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--from-journal", action="store_true",
                       help="derive the count from journalctl (preferred)")
    group.add_argument("--spurious", type=int,
                       help="subtract exactly this many, when the journal has rotated")
    ap.add_argument("--commit", action="store_true",
                    help="apply the change; without it nothing is written")
    ap.add_argument("--journal-since", default="7 days ago",
                    help="how far back to read the journal (default: %(default)s). "
                         "Widen it for an older draft; an unbounded read is slow "
                         "enough to look like a hang.")
    ap.add_argument("--force", action="store_true",
                    help="apply even though a receipt says this session was already repaired")
    args = ap.parse_args()

    receipt = receipt_for(args.session_id)
    if receipt.exists() and not args.force:
        sys.exit(f"already repaired: {receipt.read_text().strip()}\n"
                 f"Repeating a relative adjustment would subtract it twice. "
                 f"Pass --force only if you are certain it did not apply.")

    async with AsyncSessionLocal() as db:
        draft = await db.scalar(
            select(DraftSession).where(DraftSession.session_id == args.session_id))
        if draft is None:
            sys.exit(f"no draft session {args.session_id}")

        players = list(draft.team_a or []) + list(draft.team_b or [])
        if not players:
            sys.exit(f"{args.session_id} has no teams; nothing was ever counted")

        if args.from_journal:
            runs = count_stat_runs_in_journal(args.session_id, args.journal_since)
            # The draft that eventually succeeds is entitled to exactly one.
            # rooms_created_at is the flag that says it got there -- the same
            # one create_rooms_pairings itself trusts to refuse a second run.
            legitimate = 1 if draft.rooms_created_at else 0
            spurious = runs - legitimate
            print(f"journal: {runs} stat updates logged for this session, "
                  f"{legitimate} legitimate -> {spurious} spurious")
            if draft.rooms_created_at is None:
                print("  NOTE: rooms_created_at is still unset, so the draft has not\n"
                      "        completed room creation yet. Every run so far was\n"
                      "        spurious, and the successful one will add its own.")
        else:
            spurious = args.spurious

        if spurious <= 0:
            print(f"nothing to undo (spurious={spurious})")
            return

        print(f"\nsession {args.session_id}  guild {draft.guild_id}  "
              f"type {draft.session_type}")
        print(f"subtracting {spurious} from each of {len(players)} players\n")
        print(f"{'player_id':<22}{'name':<26}{'before':>8}{'after':>8}")

        changed = []
        for pid in players:
            stat = await db.scalar(select(PlayerStats).where(
                PlayerStats.player_id == pid,
                PlayerStats.guild_id == draft.guild_id))
            if stat is None:
                print(f"{pid:<22}{'(no player_stats row)':<26}{'-':>8}{'-':>8}")
                continue
            before = stat.drafts_participated or 0
            after = before - spurious
            if after < 0:
                sys.exit(f"\nREFUSING: {pid} would go to {after}. The count is "
                         f"wrong; nothing has been written.")
            print(f"{pid:<22}{(stat.display_name or '?'):<26}{before:>8}{after:>8}")
            changed.append((stat, after))

        if not args.commit:
            print(f"\nDRY RUN -- nothing written. Re-run with --commit to apply.")
            return

        for stat, after in changed:
            stat.drafts_participated = after
        await db.commit()
        RECEIPTS.mkdir(exist_ok=True)
        receipt.write_text(
            f"{datetime.now().isoformat(timespec='seconds')} "
            f"subtracted {spurious} from {len(changed)} players "
            f"in guild {draft.guild_id}\n")
        print(f"\ncommitted: {len(changed)} players adjusted by -{spurious}")
        print(f"receipt: {receipt}")


if __name__ == "__main__":
    asyncio.run(main())
