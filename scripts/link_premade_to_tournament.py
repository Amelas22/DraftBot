"""Retro-link in-progress premade drafts to their tournament matches.

Premade drafts started with /premade_draft (instead of the ▶ Play this match
button) have no tournament_match_id, so their results never auto-record. This
sets tournament_match_id on each in-progress league draft to the matching
TournamentMatch — so when the draft finishes, the existing victory hook records
the result automatically, exactly as the Play button would have.

Matching: each draft team name is fuzzy-matched (difflib) to one of the
tournament's participants; the two resolved teams must form a single unreported
match. Orientation is fixed so the draft's team A lines up with the match's
team A (the match's participant order is swapped if needed) — otherwise the
score would record backwards.

    pipenv run python scripts/link_premade_to_tournament.py --preview [--guild ID]
    pipenv run python scripts/link_premade_to_tournament.py --apply   [--guild ID]

--preview reads only. --apply must run where the bot's drafts.db is (the droplet).
"""
import argparse
import asyncio
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from database.db_session import get_session_factory
from models.draft_session import DraftSession
from models.tournament import Tournament, TournamentMatch
from services.tournament_linking import resolve_candidate_matches

GUILD_ID = "336345350535118849"


def _ids(value):
    """DraftSession.team_a/b is a JSON column → already a list (or a str/None)."""
    if isinstance(value, str):
        value = json.loads(value or "[]")
    return set(value or [])


def _game_wins(con, session_id, team_a_ids, team_b_ids):
    wins = con.execute(
        "SELECT winner_id FROM match_results WHERE session_id=? AND winner_id IS NOT NULL",
        (session_id,),
    ).fetchall()
    a = sum(1 for (w,) in wins if w in team_a_ids)
    b = sum(1 for (w,) in wins if w in team_b_ids)
    return a, b


async def plan_links(session, guild_id, db_path):
    """Work out the link for each in-progress league premade draft."""
    tournament = (await session.execute(
        select(Tournament).where(Tournament.guild_id == str(guild_id),
                                 Tournament.status.in_(("registration", "active")))
    )).scalars().first()
    if tournament is None:
        raise SystemExit(f"No active tournament for guild {guild_id}.")

    drafts = (await session.execute(
        select(DraftSession).where(
            DraftSession.guild_id == str(guild_id),
            DraftSession.session_type == "premade",
            DraftSession.tournament_match_id.is_(None),
            DraftSession.session_stage != "completed",
            DraftSession.draft_start_time >= "2026-06-01",
        )
    )).scalars().all()

    con = sqlite3.connect(db_path)
    plans = []
    for d in drafts:
        candidates = await resolve_candidate_matches(
            session, tournament, d.team_a_name, d.team_b_name)
        ga, gb = _game_wins(con, d.session_id, _ids(d.team_a), _ids(d.team_b))
        if len(candidates) != 1:
            note = ("no candidate match" if not candidates
                    else f"ambiguous ({len(candidates)} candidate matches)")
            plans.append({
                "session_id": d.session_id, "a_name": d.team_a_name,
                "b_name": d.team_b_name, "match": None, "orient": "normal",
                "score": (ga, gb), "note": note,
            })
            continue
        c = candidates[0]
        match = await session.get(TournamentMatch, c.match_id)
        plans.append({
            "session_id": d.session_id, "a_name": d.team_a_name, "b_name": d.team_b_name,
            "match": match, "orient": "reversed" if c.reversed else "normal",
            "score": (ga, gb), "note": None,
        })
    con.close()
    return tournament, plans


def _print_plans(plans):
    for p in plans:
        ga, gb = p["score"]
        if p["note"]:
            print(f"  ⚠ SKIP  {p['a_name']!r} vs {p['b_name']!r}  ({ga}-{gb})  — {p['note']}")
        else:
            arrow = "(reversed → match order will be swapped)" if p["orient"] == "reversed" else ""
            print(f"  ✓ LINK  {p['a_name']!r} {ga}-{gb} {p['b_name']!r}  → match {p['match'].id} "
                  f"{arrow}")
    linkable = sum(1 for p in plans if not p["note"])
    print(f"\n{linkable}/{len(plans)} drafts linkable; {len(plans)-linkable} skipped.")


async def run(guild_id, apply, db_path):
    factory = get_session_factory()
    async with factory() as session:
        tournament, plans = await plan_links(session, guild_id, db_path)
        print(f"Tournament: {tournament.name} [{tournament.status}] guild {guild_id}\n")
        _print_plans(plans)
        if not apply:
            print("\n(preview only — nothing written. Re-run with --apply on the bot's host.)")
            return
        for p in plans:
            if p["note"]:
                continue
            match = await session.get(TournamentMatch, p["match"].id)
            if p["orient"] == "reversed":
                match.team_a_participant_id, match.team_b_participant_id = (
                    match.team_b_participant_id, match.team_a_participant_id)
            draft = (await session.execute(
                select(DraftSession).where(DraftSession.session_id == p["session_id"])
            )).scalars().one()
            draft.tournament_match_id = match.id
        await session.commit()
        print("\n✅ Linked. Finishing drafts will now auto-record into the tournament.")


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--apply", action="store_true")
    ap.add_argument("--guild", default=GUILD_ID)
    args = ap.parse_args()
    # db path for the raw score query (matches the ORM's DATABASE_URL)
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "drafts.db")
    asyncio.run(run(args.guild, args.apply, db_path))


if __name__ == "__main__":
    main()
