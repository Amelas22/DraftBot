"""THE ledger fold behind every player-facing statistic.

/stats, /record head-to-head, and the leaderboards all count from
match_results (the source of truth the rating system already uses) via
LedgerSnapshot/fetch_session_records — never from display artifacts
(sign_ups JSON, victory-message ids, trophy_drafters name strings), whose
absence or decoration produced systematic undercounts. Scope is
RATING_SESSION_TYPES: exactly the drafts TrueSkill rates.

Depends only on sqlalchemy -- transitively via models/helpers -- so it is
importable anywhere, including migrations.
"""
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from sqlalchemy import select

from database.db_session import db_session
from helpers.legacy_import import LEGACY_SESSION_PREFIX
from helpers.skill import RATING_SESSION_TYPES, is_valid_match
from models.draft_session import DraftSession
from models.match import MatchResult


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One (player, session) aggregate over reported rated matches -- the
    typed contract the fold emits (via LedgerSnapshot.fold /
    fetch_session_records). Frozen/slots with read-only collection fields
    (`window_opponents` is a MappingProxyType of opp_id -> (wins, losses)
    tuples; `participants`/`teammates` are frozensets): records are fanned
    out to every /stats, /record, and leaderboard projection, so nothing
    downstream can mutate a shared record out from under another consumer.

    `side` is 'a' | 'b' | None; None means side inference failed for this
    player, and side_wins/side_losses are then None too -- see
    side_eligible for the one policy governing side=None records.
    """
    player_id: str
    session_id: str
    session_type: str
    cube: str | None
    completed: bool
    started_at: datetime | None
    wins: int
    matches: int
    side: str | None
    side_wins: int | None
    side_losses: int | None
    fits_window: bool
    window_wins: int
    window_matches: int
    window_opponents: MappingProxyType
    participants: frozenset
    teammates: frozenset


def _is_completed(session_row) -> bool:
    """Draft-level completion predicate (spec, verbatim policy)."""
    return (
        session_row.session_stage == "completed"
        or session_row.victory_message_id_results_channel is not None
        or (session_row.session_id or "").startswith(LEGACY_SESSION_PREFIX)
    )


def _side_map(session_row, matches) -> dict:
    """player_id -> 'a'|'b'. Native sessions use team_a/team_b JSON; legacy
    sessions (no teams) use match positions: player1s are side 'a'.

    Native sessions can also have participants missing from team_a/team_b --
    e.g. a substitute added via /add_sub after the team JSON was written.
    _infer_unlisted_sides fills those in from who they played.
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
    """Fixed-point propagation for participants absent from team_a/team_b:
    a player who faced an opponent with a known side takes the opposite
    side, repeated until a pass assigns nothing more.

    A participant who only ever faced other unlisted players can't be
    inferred and is left out of `sides`. Their matches then can't be
    attributed to either side, so the fold excludes those matches from the
    session's side tally (keeping every OTHER player's side_wins/
    side_losses accurate), and the unresolved player's own record is
    emitted with side=None -- see side_eligible for how consumers must
    treat it.
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

    Side is the source of truth -- never opponents-absence, which would
    misclassify an opposing player pid simply never got paired against
    (the common case in a 4v4, where 3 rounds only pair each player
    against 3 of their 4 opponents). A participant whose own side is
    unresolved never enters a resolved side's teammate set. The
    opponents-absence heuristic survives only when PID's side is
    unresolved -- that record is side=None and side-dependent consumers
    skip it (see side_eligible), so its teammate set is informational.
    """
    my_side = sides.get(pid)
    teammates = set()
    for p in participants:
        if p == pid:
            continue
        their_side = sides.get(p)
        if my_side is not None:
            if their_side == my_side:
                teammates.add(p)
        elif p not in opponents:
            teammates.add(p)
    return teammates


async def fetch_guild_rows(guild_id: str) -> list:
    """One SQL fetch of a guild's whole rated reported history (labeled
    column rows, never ORM entities -- materializing entity pairs measured
    ~10s on prod scale). The query deliberately takes no player/since
    params: callers that need several views of the same guild fetch once
    and fold repeatedly."""
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
                DraftSession.session_type.in_(RATING_SESSION_TYPES),
                MatchResult.winner_id.isnot(None),
            )
            .order_by(MatchResult.id)
        )).all()


def _event_time(row):
    """When a match 'happened' for windowing: COALESCE(result_submitted_at,
    draft_start_time). None (possible on odd legacy rows) can never be
    inside a bounded window."""
    return row.result_submitted_at or row.draft_start_time


class LedgerSnapshot:
    """An opaque, already-fetched view of one guild's whole rated reported
    history, wrapped so callers never see or hold onto the row shape
    directly. Rows are grouped into per-session buckets ONCE here (the
    fold-invariant work: validity guard, participants, max event time);
    `.fold()` then skips whole sessions that can't contribute to the
    requested player/window view before doing any per-session work, so a
    weekly or single-player fold costs a fraction of a lifetime one.
    Multi-timeframe callers (three timeframes of /stats or /record) should
    `await LedgerSnapshot.fetch(guild_id)` once and `.fold()` per view
    instead of re-fetching per view.
    """

    def __init__(self, rows: list):
        self._sessions = _group_sessions(rows)

    @classmethod
    async def fetch(cls, guild_id: str) -> "LedgerSnapshot":
        rows = await fetch_guild_rows(guild_id)
        # Group off the event loop: at prod scale (~22k rows) grouping is
        # hundreds of ms of pure Python that would stall every other
        # interaction in this single-process bot.
        return await asyncio.to_thread(cls, rows)

    def fold(self, player_id: str = None, since=None) -> list[SessionRecord]:
        return _fold_grouped(self._sessions, player_id=player_id, since=since)


async def fetch_session_records(guild_id: str, player_id: str = None,
                                since=None) -> list[SessionRecord]:
    """LedgerSnapshot.fetch + .fold in one call, for callers that need a
    single view. Multi-timeframe callers should fetch a snapshot once and
    fold per timeframe instead of calling this repeatedly."""
    snapshot = await LedgerSnapshot.fetch(guild_id)
    return snapshot.fold(player_id=player_id, since=since)


def _group_sessions(rows) -> dict:
    """Group raw ledger rows into per-session buckets -- the fold-invariant
    work (validity guard, participants, max event time), done once per
    snapshot so every .fold() can reuse it. Rows are stored as-is: each
    labeled row from fetch_guild_rows carries both the match and session
    columns the fold reads.
    Guards mirror helpers.skill.backfill_skill_ratings (shared
    is_valid_match predicate)."""
    by_session: dict[str, dict] = {}
    for row in rows:
        if not is_valid_match(row.player1_id, row.player2_id, row.winner_id):
            continue
        bucket = by_session.setdefault(row.session_id, {
            "matches": [], "participants": set(), "max_event": None})
        bucket["matches"].append(row)
        bucket["participants"].update((row.player1_id, row.player2_id))
        event_time = _event_time(row)
        if event_time is not None and (bucket["max_event"] is None
                                       or event_time > bucket["max_event"]):
            bucket["max_event"] = event_time
    return by_session


def _fold_grouped(by_session: dict, player_id: str = None, since=None) -> list[SessionRecord]:
    """The fold body over pre-grouped session buckets.

    since filters individual matches by their event time; records with no
    in-window matches are omitted. When since is None (lifetime), that
    filter doesn't run at all, so matches with a NULL event time are kept
    -- deliberately the same outcome as passing a since so far in the past
    it precedes all data, so every lifetime caller (/stats, h2h,
    leaderboards) sees the same lifetime set regardless of which of those
    two spellings it used to pass.

    Sessions that can't contribute to the requested view are skipped
    before any per-session work (side inference, teammate computation):
    the requested player isn't a participant, or no match event reaches
    the window (equivalent to every emitted record being dropped by the
    window_matches==0 filter below, just without paying for it first).
    """
    records = []
    for session_id, bucket in by_session.items():
        if player_id is not None and player_id not in bucket["participants"]:
            continue
        if since is not None and (bucket["max_event"] is None
                                  or bucket["max_event"] < since):
            continue
        matches = bucket["matches"]
        session_row = matches[0]    # session columns repeat on every row
        window_flags = [since is None or
                        (_event_time(m) is not None and _event_time(m) >= since)
                        for m in matches]
        # Owner-ruled windowing: whole-session facts are all-or-nothing —
        # the session "fits" only when EVERY match event lies inside the
        # window. Straddlers contribute their in-window matches to match
        # totals but nothing at the draft level (no partial-session draft
        # outcomes, e.g. a 5-1 session must never read as a windowed tie).
        fits_window = all(window_flags)
        sides = _side_map(session_row, matches)
        side_tally = {"a": 0, "b": 0}
        per_player: dict[str, dict] = {}
        for m, in_window in zip(matches, window_flags):
            winner = m.winner_id
            loser = m.player2_id if winner == m.player1_id else m.player1_id
            winner_side = sides.get(winner)
            if winner_side:
                side_tally[winner_side] += 1
            for me, opp, won in ((winner, loser, True), (loser, winner, False)):
                rec = per_player.setdefault(me, {
                    "wins": 0, "losses": 0, "opponents": {},
                    "window_wins": 0, "window_matches": 0,
                    "window_opponents": {}})
                rec["wins" if won else "losses"] += 1
                pair = rec["opponents"].setdefault(opp, [0, 0])
                pair[0 if won else 1] += 1
                if in_window:
                    rec["window_matches"] += 1
                    rec["window_wins"] += 1 if won else 0
                    wpair = rec["window_opponents"].setdefault(opp, [0, 0])
                    wpair[0 if won else 1] += 1

        total = side_tally["a"] + side_tally["b"]
        completed = _is_completed(session_row)
        participants = set(per_player.keys())
        for pid, rec in per_player.items():
            if player_id is not None and pid != player_id:
                continue
            if rec["window_matches"] == 0 and not fits_window:
                continue    # nothing of this session touches the window
            my_side = sides.get(pid)
            if my_side:
                side_wins = side_tally.get(my_side, 0)
                side_losses = total - side_wins
            else:
                # Unrepresentable rather than fabricated: a side=None
                # record has no side outcome, so reading these as numbers
                # must throw, not miscount (see side_eligible).
                side_wins = side_losses = None
            records.append(SessionRecord(
                player_id=pid,
                session_id=session_id,
                session_type=session_row.session_type,
                cube=session_row.cube,
                completed=completed,
                started_at=session_row.draft_start_time,
                wins=rec["wins"],
                matches=rec["wins"] + rec["losses"],
                side=my_side,
                side_wins=side_wins,
                side_losses=side_losses,
                fits_window=fits_window,
                window_wins=rec["window_wins"],
                window_matches=rec["window_matches"],
                window_opponents=MappingProxyType(
                    {opp: tuple(pair)
                     for opp, pair in rec["window_opponents"].items()}),
                participants=frozenset(participants),
                teammates=frozenset(_compute_teammates(
                    pid, participants, sides, rec["opponents"])),
            ))
    records.sort(key=lambda r: (r.started_at or datetime.min, r.session_id))
    return records


def match_totals(records) -> dict:
    """Every reported IN-WINDOW match counts — same moment ratings move.
    (With no window, window totals equal full-session totals.)
    Side-independent: counts side=None records normally."""
    return {
        "matches_played": sum(r.window_matches for r in records),
        "matches_won": sum(r.window_wins for r in records),
    }


def draft_totals(records) -> int:
    """Completed sessions that fit the window entirely (owner ruling:
    whole-session facts are all-or-nothing per window).
    Side-independent: counts side=None records normally."""
    return sum(1 for r in records if r.completed and r.fits_window)


# Whole-session facts (trophies, team outcomes) are all-or-nothing per
# window (owner ruling): a session contributes them only when it fits the
# window entirely (fits_window), and always with its FULL record — partial
# slices of a session never produce draft-level outcomes.
def trophy_count(records) -> int:
    """Undefeated completed drafts with a full 3+ match slate.
    Side-independent: counts side=None records normally."""
    return sum(1 for r in records
               if r.completed and r.fits_window
               and r.wins == r.matches >= 3)


def side_eligible(record) -> bool:
    """THE side=None policy, in one place. A record whose side could not
    be resolved (side=None, side_wins/side_losses=None) carries no team
    outcome: side-DEPENDENT projections (team_record, h2h_totals' draft
    splits, the leaderboard teammate pass) gate through this predicate --
    the session must be completed, fit the window entirely, and have a
    resolved side -- and skip ineligible records rather than fabricate a
    loss/tie. Side-INDEPENDENT projections (match_totals, draft_totals,
    trophy_count, cube_breakdown) deliberately don't use this gate and
    count side=None records like any other."""
    return record.completed and record.fits_window and record.side is not None


def side_outcome(record) -> str:
    """'won' | 'lost' | 'tied' for the record-owner's side of one session.
    THE won/lost/tied policy. Only meaningful behind a side_eligible
    check; on a side=None record the None comparisons raise rather than
    miscount."""
    if record.side_wins > record.side_losses:
        return "won"
    if record.side_wins < record.side_losses:
        return "lost"
    return "tied"


def team_record(records) -> dict:
    """Side-dependent (gated by side_eligible)."""
    tally = {"won": 0, "lost": 0, "tied": 0}
    for r in records:
        if side_eligible(r):
            tally[side_outcome(r)] += 1
    return {"played": sum(tally.values()), **tally}


def cube_breakdown(records) -> dict:
    """Per-cube totals, grouped case-insensitively -- prod has LSVCube /
    Lsvcube / lsvcube variants of the same cube that must roll up together
    rather than split the same cube's stats across separate entries.
    Each group displays under whichever exact spelling was used most often
    (ties broken by whichever is most recent, since records arrive from
    the fold in started_at order). Sessions with no cube set are grouped
    under the key None -- the display layer decides what, if anything, to
    render for them (a sentinel name here could shadow a real cube).

    drafts counts completed records only, consistent with draft_totals --
    an in-progress draft shouldn't count toward either.
    Side-independent: counts side=None records normally.
    """
    groups: dict = {}
    for r in records:
        key = r.cube.lower() if r.cube else None
        group = groups.setdefault(key, {
            "wins": 0, "losses": 0, "drafts": 0, "spelling_counts": {}})
        group["wins"] += r.window_wins
        group["losses"] += r.window_matches - r.window_wins
        if r.completed and r.fits_window:
            group["drafts"] += 1
        if r.cube:
            group["spelling_counts"][r.cube] = group["spelling_counts"].get(r.cube, 0) + 1
            group["last_spelling"] = r.cube

    cubes: dict = {}
    for key, group in groups.items():
        spelling_counts = group.pop("spelling_counts")
        last_spelling = group.pop("last_spelling", None)
        display_spelling = None if key is None else max(
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
    sides, computed once by _compute_teammates) -- NOT from whether
    opponent_id happens to appear among the record's window opponents; in
    a 4v4 an opposing player you simply never got paired against would be
    absent there despite being on the other side.

    matches_played/matches_won (direct match totals) are side-independent
    and always counted; the with/against draft split is side-dependent
    (gated by side_eligible).
    """
    matches_played = matches_won = 0
    drafts_with = drafts_against = drafts_with_won = drafts_against_won = 0
    drafts_with_tied = drafts_against_tied = 0
    for r in records:
        pair = r.window_opponents.get(opponent_id)
        if pair:
            matches_won += pair[0]
            matches_played += pair[0] + pair[1]
        if not side_eligible(r) or opponent_id not in r.participants:
            continue
        outcome = side_outcome(r)
        if opponent_id in r.teammates:              # same side
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
