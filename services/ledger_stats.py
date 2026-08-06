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
from collections import namedtuple
from datetime import datetime

from sqlalchemy import select

from database.db_session import db_session
from helpers.skill import RATING_SESSION_TYPES
from models.draft_session import DraftSession
from models.match import MatchResult

RATED_TYPES = RATING_SESSION_TYPES

# Lightweight stand-ins for the ORM rows the fold used to materialize.
# fetch_session_records selects only these columns -- see its docstring --
# so _side_map/_is_completed/_infer_unlisted_sides only ever need attribute
# access on these tuples, never the full MatchResult/DraftSession entities.
_MatchRow = namedtuple(
    "_MatchRow", "player1_id player2_id winner_id result_submitted_at id")
_SessionRow = namedtuple(
    "_SessionRow", "session_id session_type session_stage "
    "victory_message_id_results_channel team_a team_b cube draft_start_time")


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


def _compute_teammates(pid, participants, sides, opponents) -> set:
    """Session-mates who share pid's side, excluding pid itself.

    This is the fix for the "never faced => teammate" bug: an opposing
    player pid simply never happened to play (the common case in a 4v4,
    where 3 rounds only pair each player against 3 of their 4 opponents)
    must NOT be classified as a teammate just because they're absent from
    `opponents`. Side is the source of truth; `opponents` only breaks ties.

    Falls back to the old opponents-absence heuristic (never a recorded
    opponent => teammate) for any pairing where a side can't be resolved
    for pid or for the other participant -- i.e. participants left out of
    `sides` entirely by _infer_unlisted_sides because they only ever faced
    other side-unresolved players. That's the rare case; for everyone else
    (the vast majority, including every 4v4 participant with a full
    team_a/team_b listing) the real side decides it.
    """
    my_side = sides.get(pid)
    teammates = set()
    for p in participants:
        if p == pid:
            continue
        their_side = sides.get(p)
        if my_side is not None and their_side is not None:
            if their_side == my_side:
                teammates.add(p)
            # else: resolved opposite side -> opponent, never a teammate,
            # regardless of whether they ever actually played each other.
        elif p not in opponents:
            teammates.add(p)
    return teammates


async def fetch_guild_rows(guild_id: str) -> list:
    """One SQL fetch of a guild's whole rated reported history (column
    tuples, never ORM entities -- materializing entity pairs measured ~10s
    on prod scale). The query deliberately takes no player/since params:
    callers that need several views of the same guild (three timeframes of
    /stats or /record) fetch once and fold repeatedly."""
    async with db_session() as s:
        return (await s.execute(
            select(
                MatchResult.player1_id, MatchResult.player2_id,
                MatchResult.winner_id, MatchResult.result_submitted_at,
                MatchResult.id,
                DraftSession.session_id, DraftSession.session_type,
                DraftSession.session_stage,
                DraftSession.victory_message_id_results_channel,
                DraftSession.team_a, DraftSession.team_b,
                DraftSession.cube, DraftSession.draft_start_time,
            )
            .join(DraftSession, MatchResult.session_id == DraftSession.session_id)
            .where(
                DraftSession.guild_id == guild_id,
                DraftSession.session_type.in_(RATED_TYPES),
                MatchResult.winner_id.isnot(None),
            )
            .order_by(MatchResult.id)
        )).all()


async def fetch_session_records(guild_id: str, player_id: str = None,
                                since=None) -> list[dict]:
    """fetch_guild_rows + fold_session_records in one call, for callers
    that need a single view. Multi-timeframe callers should fetch rows once
    and fold per timeframe instead of calling this repeatedly."""
    return fold_session_records(await fetch_guild_rows(guild_id),
                                player_id=player_id, since=since)


def fold_session_records(rows, player_id: str = None, since=None) -> list[dict]:
    """Per-(player, session) aggregates over reported rated matches (pure).

    since filters individual matches by COALESCE(result_submitted_at,
    draft_start_time); records with no in-window matches are omitted. When
    since is None (lifetime), that filter doesn't run at all, so matches
    with a NULL event time are kept -- deliberately the same outcome as
    passing a since so far in the past it precedes all data, so every
    lifetime caller (/stats, h2h, leaderboards) sees the same lifetime set
    regardless of which of those two spellings it used to pass.
    Guards mirror helpers.skill.backfill_skill_ratings.
    """
    by_session: dict[str, dict] = {}
    for row in rows:
        match = _MatchRow(row.player1_id, row.player2_id, row.winner_id,
                           row.result_submitted_at, row.id)
        p1, p2, w = match.player1_id, match.player2_id, match.winner_id
        if not p1 or not p2 or p1 == p2 or w not in (p1, p2):
            continue
        event_time = match.result_submitted_at or row.draft_start_time
        if since is not None and event_time is not None and event_time < since:
            continue
        if since is not None and event_time is None:
            continue
        bucket = by_session.setdefault(row.session_id, {
            "session_row": None, "matches": []})
        if bucket["session_row"] is None:
            bucket["session_row"] = _SessionRow(
                row.session_id, row.session_type, row.session_stage,
                row.victory_message_id_results_channel, row.team_a,
                row.team_b, row.cube, row.draft_start_time)
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
                "teammates": _compute_teammates(
                    pid, participants, sides, rec["opponents"]),
            })
    records.sort(key=lambda r: (r["started_at"] or datetime.min, r["session_id"]))
    return records


def match_totals(records) -> dict:
    """Every reported match counts — same moment ratings move."""
    return {
        "matches_played": sum(r["matches"] for r in records),
        "matches_won": sum(r["wins"] for r in records),
    }


def draft_totals(records) -> int:
    """Completed sessions only (spec's completion predicate)."""
    return sum(1 for r in records if r["completed"])


# trophy_count and team_record both classify a whole draft (undefeated? which
# side won?) from records built by fetch_session_records, which itself may
# have been called with since= -- a window that includes only the matches
# reported inside it, not the whole session. A session straddling the window
# boundary (started before `since`, finished after it, or vice versa) is
# classified here from its in-window matches only, so a draft that reads as
# a trophy/win in a windowed timeframe can differ from its lifetime
# classification (built from every match). That's accepted, deliberate
# per-match-windowing semantics -- the alternative (pulling in out-of-window
# matches to reclassify a session) would make "last 7 days" no longer mean
# "what happened in the last 7 days."
def trophy_count(records) -> int:
    """Undefeated completed drafts with a full 3+ match slate."""
    return sum(1 for r in records
               if r["completed"] and r["wins"] == r["matches"] >= 3)


def side_outcome(record) -> str:
    """'won' | 'lost' | 'tied' for the record-owner's side of one session.
    THE won/lost/tied policy -- team_record, h2h_totals, and the
    leaderboard's teammate pass all classify through here."""
    if record["side_wins"] > record["side_losses"]:
        return "won"
    if record["side_wins"] < record["side_losses"]:
        return "lost"
    return "tied"


def team_record(records) -> dict:
    tally = {"won": 0, "lost": 0, "tied": 0}
    for r in records:
        if r["completed"]:
            tally[side_outcome(r)] += 1
    return {"played": sum(tally.values()), **tally}


def cube_breakdown(records) -> dict:
    """Per-cube totals, grouped case-insensitively -- prod has LSVCube /
    Lsvcube / lsvcube variants of the same cube that must roll up together
    rather than split the same cube's stats across separate entries.
    Each group displays under whichever exact spelling was used most often
    (ties broken by whichever is most recent, since records arrive from
    fetch_session_records in started_at order).

    drafts counts completed records only, consistent with draft_totals and
    the /stats embed's "min 5 drafts" display threshold -- an in-progress
    draft shouldn't count toward either.
    """
    groups: dict[str, dict] = {}
    for r in records:
        cube = r["cube"] or "Unknown"
        group = groups.setdefault(cube.lower(), {
            "wins": 0, "losses": 0, "drafts": 0, "spelling_counts": {}})
        group["wins"] += r["wins"]
        group["losses"] += r["losses"]
        if r["completed"]:
            group["drafts"] += 1
        group["spelling_counts"][cube] = group["spelling_counts"].get(cube, 0) + 1
        group["last_spelling"] = cube

    cubes: dict[str, dict] = {}
    for group in groups.values():
        spelling_counts = group.pop("spelling_counts")
        last_spelling = group.pop("last_spelling")
        display_spelling = max(
            spelling_counts,
            key=lambda s: (spelling_counts[s], s == last_spelling))
        cubes[display_spelling] = group
    return cubes


def h2h_totals(records, opponent_id: str) -> dict:
    """Per-opponent totals, three-way split (won/tied/not) mirroring
    team_record -- a tied shared session must read as "tied" everywhere,
    not silently collapse into a loss in one projection and a tie in
    another.

    with/against is decided from each record's `teammates` set (real
    sides, computed once by fetch_session_records/_compute_teammates) --
    NOT from whether opponent_id shows up in `opponents`. In a 4v4 an
    opposing player you simply never got paired against in a round would
    be absent from `opponents` despite being on the other side; treating
    that absence as "teammates" (the old heuristic) misclassified them.
    """
    matches_played = matches_won = 0
    drafts_with = drafts_against = drafts_with_won = drafts_against_won = 0
    drafts_with_tied = drafts_against_tied = 0
    for r in records:
        pair = r["opponents"].get(opponent_id)
        if pair:
            matches_won += pair[0]
            matches_played += pair[0] + pair[1]
        if not r["completed"] or opponent_id not in r["participants"]:
            continue
        outcome = side_outcome(r)
        if opponent_id in r["teammates"]:          # same side
            drafts_with += 1
            if outcome == "won":
                drafts_with_won += 1
            elif outcome == "tied":
                drafts_with_tied += 1
        else:                                      # opposing side
            drafts_against += 1
            if outcome == "won":
                drafts_against_won += 1
            elif outcome == "tied":
                drafts_against_tied += 1
    return {
        "matches_played": matches_played, "matches_won": matches_won,
        "drafts_with": drafts_with, "drafts_against": drafts_against,
        "drafts_with_won": drafts_with_won,
        "drafts_against_won": drafts_against_won,
        "drafts_with_tied": drafts_with_tied,
        "drafts_against_tied": drafts_against_tied,
    }
