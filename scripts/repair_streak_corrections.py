"""One-off: heal streaks left wrong by corrections made before the fix.

Until commit 1ae8f67, streak bookkeeping ran only on a first report. A
corrected result took one of the other two paths and never revisited streaks,
so a 2-0 fixed to 2-1 left a perfect streak standing on a sweep that no longer
existed, and a flipped winner left the mis-credited player's streak running
through their own loss -- or left the real winner's streak broken by a loss
that was never theirs.

The shipped fix is forward-looking: affected players stay wrong until someone
happens to correct one of their matches again. This repairs them directly with
the same backfill_streaks the live path now calls, scoped per player so nobody
else's records are touched.

Not every disagreement with the ledger is this bug, and the others are
deliberately left alone:

  * never populated -- all four streak columns are 0 for a player who has
    rated results. Their stats predate the streak feature; nothing corrected
    them, so nothing here should.
  * old-backfill shape -- the stored values are exactly what the ledger gives
    when restricted to random+staked, the only types the original backfill
    scripts covered. The gap is the premade matches those scripts never saw,
    not a correction.

Only players matching neither description are repaired.

Dry run by default. Read the report, then re-run with --apply.

    pipenv run python scripts/repair_streak_corrections.py --db drafts.db
    pipenv run python scripts/repair_streak_corrections.py --db drafts.db --apply
"""
import argparse
import sys

from sqlalchemy import create_engine, text

sys.path.insert(0, ".")
import helpers.skill as skill  # noqa: E402

FIELDS = ("current_win_streak", "current_perfect_streak",
          "longest_win_streak", "longest_perfect_streak")
CURRENT = FIELDS[:2]
OLD_BACKFILL_TYPES = ("random", "staked")


def read_all(conn):
    cols = ", ".join(FIELDS)
    return {(r[0], r[1]): dict(zip(FIELDS, [v or 0 for v in r[2:]]))
            for r in conn.execute(text(f"SELECT player_id, guild_id, {cols} FROM player_stats"))}


def derive(engine, session_types):
    """What backfill_streaks would write for every player, without writing it."""
    original = skill.RATING_SESSION_TYPES
    skill.RATING_SESSION_TYPES = session_types
    try:
        with engine.connect() as conn:
            trans = conn.begin()
            stored = read_all(conn)
            skill.backfill_streaks(conn)
            derived = read_all(conn)
            trans.rollback()
        return stored, derived
    finally:
        skill.RATING_SESSION_TYPES = original


def describe(stored, derived, key):
    return ", ".join(
        f"{f.replace('current_', 'cur_').replace('longest_', 'max_')} "
        f"{stored[key][f]}->{derived[key][f]}"
        for f in FIELDS if stored[key][f] != derived[key][f])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="drafts.db")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    engine = create_engine(f"sqlite:///{args.db}")
    # A whole-ledger replay gives each player exactly what a per-player replay
    # would: a player's streaks depend only on their own matches.
    stored, truth = derive(engine, skill.RATING_SESSION_TYPES)
    _, old_shape = derive(engine, OLD_BACKFILL_TYPES)

    diverged = [k for k in stored
                if any(stored[k][f] != truth.get(k, {}).get(f, 0) for f in CURRENT)]
    never = [k for k in diverged if all(stored[k][f] == 0 for f in FIELDS)]
    legacy = [k for k in diverged if k not in set(never)
              and all(stored[k][f] == old_shape.get(k, {}).get(f, 0) for f in FIELDS)]
    repair = [k for k in diverged if k not in set(never) | set(legacy)]
    high = [k for k in repair if any(stored[k][f] > truth[k][f] for f in CURRENT)]

    print(f"players: {len(stored)}   disagree with the ledger: {len(diverged)}")
    print(f"  never populated (pre-date the feature) : {len(never):>4}  left alone")
    print(f"  old random+staked-only backfill shape  : {len(legacy):>4}  left alone")
    print(f"  corrections that never healed          : {len(repair):>4}  REPAIR")
    print(f"      streak outlived a correction : {len(high)}")
    print(f"      streak killed by a misreport : {len(repair) - len(high)}")
    print()
    for key in sorted(repair):
        print(f"  {key[0]:>20}  {describe(stored, truth, key)}")

    if not args.apply:
        print(f"\nDry run. Re-run with --apply to repair {len(repair)} player(s).")
        return

    with engine.begin() as conn:
        for player_id, guild_id in repair:
            skill.backfill_streaks(conn, [player_id], guild_id)

    with engine.connect() as conn:
        after = read_all(conn)
    wrong = [k for k in repair if any(after[k][f] != truth[k][f] for f in FIELDS)]
    untouched = [k for k in stored
                 if k not in set(repair) and stored[k] != after.get(k)]
    print(f"\nRepaired {len(repair) - len(wrong)}/{len(repair)} player(s).")
    print(f"Players outside the repair set that changed: {len(untouched)} (must be 0)")
    if wrong:
        print(f"  STILL WRONG: {[k[0] for k in wrong]}")


if __name__ == "__main__":
    main()
