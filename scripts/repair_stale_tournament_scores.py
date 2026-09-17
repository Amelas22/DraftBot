"""One-off: repair tournament scores frozen at the clinch.

The draft victory chokepoint recorded a linked tournament match exactly once --
the moment the draft clinched -- because record_linked_result sat inside the
`already_processed` idempotency gate in check_and_post_victory_or_draw. These
are 9-pairing matches, so a team clinches at 5 wins and the remaining games are
still played and reported afterwards. The tournament kept the clinch score.

Lotus League 2026 example: 🔥 vs The Book Club was recorded 5-2 and finished
7-2. Match wins and points are unaffected (no winner changes when the trailing
games land), but game_wins/game_losses -- shown on the league site and used as a
tiebreaker -- are understated for most matches.

This recomputes each match from its linked draft using the bot's own
calculate_team_wins, and rewrites it through services.tournament_service.
set_result, which reverts the old result before applying the new one so the
participants' aggregates stay correct.

Only matches that ALREADY have a result and whose score has moved are touched;
nothing invents a result for a match still being played. A match whose winner
would change is refused unless --allow-winner-change, since that would move
points and standings rather than just the game counts.

    pipenv run python scripts/repair_stale_tournament_scores.py
    pipenv run python scripts/repair_stale_tournament_scores.py --apply

Dry run by default. --apply must run where the bot's drafts.db is (the droplet).
It does NOT refresh the Discord standings message; run /tournament status (or
correct any match) afterwards to force a re-post.
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402


async def collect(session, tournament_id=None):
    """Every recorded, non-bye match paired with its draft's true score."""
    from models.draft_session import DraftSession
    from models.tournament import TournamentMatch, TournamentParticipant, TournamentRound
    from utils import calculate_team_wins

    stmt = (select(TournamentMatch, TournamentRound)
            .join(TournamentRound, TournamentRound.id == TournamentMatch.round_id)
            .where(TournamentMatch.is_bye.is_(False),
                   TournamentMatch.team_a_wins.isnot(None))
            .order_by(TournamentRound.round_number, TournamentMatch.id))
    if tournament_id is not None:
        stmt = stmt.where(TournamentRound.tournament_id == tournament_id)

    rows = []
    for match, round_ in (await session.execute(stmt)).all():
        draft = (await session.execute(
            select(DraftSession).where(
                DraftSession.tournament_match_id == match.id))).scalars().first()
        if draft is None:
            continue
        true_a, true_b = await calculate_team_wins(draft.session_id)
        part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
        part_b = await session.get(TournamentParticipant, match.team_b_participant_id)
        rows.append({
            "match": match, "round": round_.round_number, "draft": draft,
            "stored": (match.team_a_wins, match.team_b_wins),
            "true": (true_a, true_b),
            "a": part_a.team_name if part_a else "?",
            "b": part_b.team_name if part_b else "?",
        })
    return rows


def winner_side(score):
    a, b = score
    return "a" if a > b else "b" if b > a else "draw"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "drafts.db"))
    ap.add_argument("--tournament", type=int, default=None,
                    help="restrict to one tournament id (default: all)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--allow-winner-change", action="store_true",
                    help="also rewrite matches whose winner would change")
    args = ap.parse_args()

    from database.db_session import AsyncSessionLocal
    engine = create_async_engine(f"sqlite+aiosqlite:///{args.db}")
    AsyncSessionLocal.configure(bind=engine)

    from services.tournament_service import set_result

    async with AsyncSessionLocal() as session:
        rows = await collect(session, args.tournament)

    stale = [r for r in rows if r["stored"] != r["true"]]
    flips = [r for r in stale if winner_side(r["stored"]) != winner_side(r["true"])]
    safe = [r for r in stale if r not in flips]

    print(f"recorded matches with a linked draft: {len(rows)}")
    print(f"  stale (stored != draft):            {len(stale)}")
    print(f"  of those, winner would change:      {len(flips)}"
          f"{'  (skipped without --allow-winner-change)' if flips and not args.allow_winner_change else ''}")
    print()
    for r in stale:
        mark = "  !! WINNER CHANGES" if r in flips else ""
        print(f"  r{r['round']} m{r['match'].id:<4} {str(r['a'])[:22]:22s} vs {str(r['b'])[:22]:22s}"
              f"  {r['stored'][0]}-{r['stored'][1]} -> {r['true'][0]}-{r['true'][1]}{mark}")

    todo = stale if args.allow_winner_change else safe
    if not args.apply:
        print(f"\nDry run. Re-run with --apply to repair {len(todo)} match(es).")
        return

    done = failed = 0
    async with AsyncSessionLocal() as session:
        for r in todo:
            try:
                await set_result(session, r["match"].id, r["true"][0], r["true"][1])
                await session.commit()
                done += 1
            except ValueError as e:
                await session.rollback()
                failed += 1
                print(f"  REFUSED m{r['match'].id}: {e}")
    print(f"\nRepaired {done}/{len(todo)} match(es)" + (f", {failed} refused." if failed else "."))
    print("Discord standings are NOT refreshed by this script -- run /tournament status.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
