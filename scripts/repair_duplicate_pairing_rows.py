"""One-off: remove duplicate match_results rows left by a double pairing run.

create_rooms_pairings guards on session.rooms_created_at, which is stamped at
the very END of the transaction that creates the rooms (views.py:1706). The
whole run -- three Discord channel creations, ~10s of API latency -- happens
inside that uncommitted transaction, so a second concurrent caller reads a
clean session row and does everything again: extra rooms, extra pairing
messages, and a second full set of match_results rows.

Two triggers race in practice. The draft ends naturally and _on_end_draft
starts pairings; seconds later a different DraftSetupManager for the same
session sees the socket drop, finds links already distributed, and calls
_handle_ownership_loss_with_pairings, which starts pairings again.

The duplicate rows make update_pairings_posting (views.py:2277) raise
MultipleResultsFound on scalar_one_or_none(), so reporting a result fails with
"Multiple rows were found when one or none was required" -- after the result
has already been written. Players see an error and a pairing message that never
updates, while the score quietly lands.

This deletes the surplus rows, keeping the one that carries the result. Player
stats are NOT affected and this does not touch them: create_team_channel
commits draft_chat_channel mid-run, so the second run reads resuming=True and
skips update_player_stats_for_draft.

    pipenv run python scripts/repair_duplicate_pairing_rows.py
    pipenv run python scripts/repair_duplicate_pairing_rows.py --session <id>
    pipenv run python scripts/repair_duplicate_pairing_rows.py --apply

Dry run by default. --apply must run where the bot's drafts.db is (the
droplet), and writes the deleted rows to a JSON file first -- production has no
automatic backup, so that file is the only way back.

It refuses a group where more than one duplicate carries a result: that means
results were split across the copies and a human has to decide which survives.
Orphaned Discord rooms from the losing run are a separate cleanup; this only
repairs the database.
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from models.match import MatchResult  # noqa: E402

DEFAULT_DB = "sqlite+aiosqlite:///drafts.db"


def has_result(row):
    """A row carries a result if anyone has won a game or the match is decided.

    Checked rather than assuming the lower id wins: the reporting path picks
    one copy to write to, and which one that is depends on row order, not on
    which run created it.
    """
    return bool(row.winner_id) or (row.player1_wins or 0) > 0 or (row.player2_wins or 0) > 0


def row_as_dict(row):
    return {
        "id": row.id,
        "session_id": row.session_id,
        "match_number": row.match_number,
        "player1_id": row.player1_id,
        "player2_id": row.player2_id,
        "player1_wins": row.player1_wins,
        "player2_wins": row.player2_wins,
        "winner_id": row.winner_id,
        "pairing_message_id": row.pairing_message_id,
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    parser.add_argument("--session", action="append", dest="sessions",
                        help="limit to this session_id (repeatable)")
    parser.add_argument("--database-url", default=DEFAULT_DB)
    args = parser.parse_args()

    engine = create_async_engine(args.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        stmt = select(MatchResult)
        if args.sessions:
            stmt = stmt.where(MatchResult.session_id.in_(args.sessions))
        rows = (await session.scalars(stmt)).all()

    groups = {}
    for row in rows:
        groups.setdefault((row.session_id, row.match_number), []).append(row)

    doomed = []
    refused = []
    for (session_id, match_number), members in sorted(groups.items()):
        if len(members) < 2:
            continue
        members.sort(key=lambda r: r.id)
        with_result = [r for r in members if has_result(r)]
        if len(with_result) > 1:
            refused.append((session_id, match_number, [r.id for r in with_result]))
            continue
        keep = with_result[0] if with_result else members[0]
        drop = [r for r in members if r.id != keep.id]
        doomed.extend(drop)
        marker = f"result {keep.player1_wins}-{keep.player2_wins}" if has_result(keep) else "no result yet"
        print(f"{session_id} match {match_number}: keep id={keep.id} ({marker}), "
              f"delete {[r.id for r in drop]}")

    for session_id, match_number, ids in refused:
        print(f"REFUSED {session_id} match {match_number}: results split across ids {ids} "
              f"-- resolve by hand")

    if not doomed:
        print("\nNothing to delete.")
        if refused:
            return 1
        return 0

    sessions_touched = sorted({r.session_id for r in doomed})
    print(f"\n{len(doomed)} surplus rows across {len(sessions_touched)} session(s).")

    if not args.apply:
        print("Dry run -- nothing written. Re-run with --apply on the droplet.")
        return 1 if refused else 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ROOT / f"duplicate_pairing_rows_deleted_{stamp}.json"
    backup.write_text(json.dumps([row_as_dict(r) for r in doomed], indent=2))
    print(f"Wrote {backup} before deleting.")

    async with Session() as session:
        await session.execute(delete(MatchResult).where(MatchResult.id.in_([r.id for r in doomed])))
        await session.commit()

    async with Session() as session:
        stmt = select(MatchResult)
        if args.sessions:
            stmt = stmt.where(MatchResult.session_id.in_(args.sessions))
        remaining = (await session.scalars(stmt)).all()
    still_duplicated = {
        key for key, members in
        ((k, [r for r in remaining if (r.session_id, r.match_number) == k]) for k in groups)
        if len(members) > 1
    }
    if still_duplicated:
        print(f"STILL DUPLICATED after delete: {sorted(still_duplicated)}")
        return 1

    print(f"Deleted {len(doomed)} rows. No duplicates remain in the inspected scope.")
    await engine.dispose()
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
