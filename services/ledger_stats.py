"""THE ledger fold behind every player-facing statistic.

/stats, /record head-to-head, and the leaderboards all count from
match_results (the source of truth the rating system already uses) via
fetch_session_records — never from display artifacts (sign_ups JSON,
victory-message ids, trophy_drafters name strings), whose absence or
decoration produced systematic undercounts. Scope is RATING_SESSION_TYPES:
exactly the drafts TrueSkill rates.

Depends only on sqlalchemy -- transitively via models/helpers -- so it is
importable anywhere, including migrations.
"""
import json
from datetime import datetime

from sqlalchemy import select

from database.db_session import db_session
from helpers.skill import RATING_SESSION_TYPES
from models.draft_session import DraftSession
from models.match import MatchResult

RATED_TYPES = RATING_SESSION_TYPES


def _is_completed(session_row) -> bool:
    """Draft-level completion predicate (spec, verbatim policy)."""
    return (
        session_row.session_stage == "completed"
        or session_row.victory_message_id_results_channel is not None
        or (session_row.session_id or "").startswith("legacy-")
    )


def _side_map(session_row, matches) -> dict:
    """player_id -> 'a'|'b'. Native sessions use team_a/team_b JSON; legacy
    sessions (no teams) use match positions: player1s are side 'a'.

    Native sessions can also have participants missing from team_a/team_b --
    e.g. a substitute added via /add_sub after the team JSON was written. For
    those, _infer_unlisted_sides fills in a side from who they played (a
    player who faced a known side takes the opposite side); see its docstring
    for what happens to the rare participant who cannot be inferred at all.
    """
    team_a, team_b = session_row.team_a, session_row.team_b
    if isinstance(team_a, str):
        team_a = json.loads(team_a)
    if isinstance(team_b, str):
        team_b = json.loads(team_b)
    sides = {}
    if team_a or team_b:
        for p in team_a or []:
            sides[p] = "a"
        for p in team_b or []:
            sides[p] = "b"
        _infer_unlisted_sides(sides, matches)
    else:
        for m in matches:
            sides.setdefault(m.player1_id, "a")
            sides.setdefault(m.player2_id, "b")
    return sides


def _infer_unlisted_sides(sides: dict, matches) -> None:
    """Fixed-point propagation for participants absent from team_a/team_b.

    A player who faced an opponent with a known side takes the opposite side;
    this repeats over the session's matches (there are at most a dozen or so)
    until a pass makes no further assignments. A participant who only ever
    faced other unlisted players can't be inferred at all and is left out of
    `sides` -- their matches then can't be attributed to either side, so
    fetch_session_records excludes those specific matches from side_tally
    (and thus from `total = side_tally['a'] + side_tally['b']`, the base
    every player's side_losses is computed from) rather than guess. That
    keeps every OTHER player's side_wins/side_losses accurate. The
    unresolved participant's own record still gets emitted (their personal
    wins/losses/opponents are unaffected) but with side_wins=0 and
    side_losses=total, since they have no side to report a session total for.
    """
    changed = True
    while changed:
        changed = False
        for m in matches:
            p1, p2 = m.player1_id, m.player2_id
            s1, s2 = sides.get(p1), sides.get(p2)
            if s1 and not s2:
                sides[p2] = "a" if s1 == "b" else "b"
                changed = True
            elif s2 and not s1:
                sides[p1] = "a" if s2 == "b" else "b"
                changed = True


async def fetch_session_records(guild_id: str, player_id: str = None,
                                since=None) -> list[dict]:
    """Per-(player, session) aggregates over reported rated matches.

    since filters individual matches by COALESCE(result_submitted_at,
    draft_start_time); records with no in-window matches are omitted.
    Guards mirror helpers.skill.backfill_skill_ratings.
    """
    async with db_session() as s:
        rows = (await s.execute(
            select(MatchResult, DraftSession)
            .join(DraftSession, MatchResult.session_id == DraftSession.session_id)
            .where(
                DraftSession.guild_id == guild_id,
                DraftSession.session_type.in_(RATED_TYPES),
                MatchResult.winner_id.isnot(None),
            )
            .order_by(MatchResult.id)
        )).all()

    by_session: dict[str, dict] = {}
    for match, session_row in rows:
        p1, p2, w = match.player1_id, match.player2_id, match.winner_id
        if not p1 or not p2 or p1 == p2 or w not in (p1, p2):
            continue
        event_time = match.result_submitted_at or session_row.draft_start_time
        if since is not None and event_time is not None and event_time < since:
            continue
        if since is not None and event_time is None:
            continue
        bucket = by_session.setdefault(session_row.session_id, {
            "session_row": session_row, "matches": []})
        bucket["matches"].append(match)

    records = []
    for session_id, bucket in by_session.items():
        session_row, matches = bucket["session_row"], bucket["matches"]
        sides = _side_map(session_row, matches)
        side_tally = {"a": 0, "b": 0}
        per_player: dict[str, dict] = {}
        for m in matches:
            winner = m.winner_id
            loser = m.player2_id if winner == m.player1_id else m.player1_id
            winner_side = sides.get(winner)
            if winner_side:
                side_tally[winner_side] += 1
            for me, opp, won in ((winner, loser, True), (loser, winner, False)):
                rec = per_player.setdefault(me, {
                    "wins": 0, "losses": 0, "opponents": {}})
                rec["wins" if won else "losses"] += 1
                pair = rec["opponents"].setdefault(opp, [0, 0])
                pair[0 if won else 1] += 1

        total = side_tally["a"] + side_tally["b"]
        completed = _is_completed(session_row)
        participants = set(per_player.keys())
        for pid, rec in per_player.items():
            if player_id is not None and pid != player_id:
                continue
            my_side = sides.get(pid)
            side_wins = side_tally.get(my_side, 0) if my_side else 0
            records.append({
                "player_id": pid,
                "session_id": session_id,
                "session_type": session_row.session_type,
                "cube": session_row.cube,
                "completed": completed,
                "started_at": session_row.draft_start_time,
                "wins": rec["wins"],
                "losses": rec["losses"],
                "matches": rec["wins"] + rec["losses"],
                "opponents": rec["opponents"],
                "side_wins": side_wins,
                "side_losses": total - side_wins,
                "participants": participants,
            })
    records.sort(key=lambda r: (r["started_at"] or datetime.min, r["session_id"]))
    return records
