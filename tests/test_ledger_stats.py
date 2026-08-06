"""The one ledger fold behind /stats, /record, and leaderboards
(spec 2026-08-06-ledger-stats-unification-design)."""
import os
import tempfile
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from database.models_base import Base
from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from models.match import MatchResult


@pytest_asyncio.fixture
async def test_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    AsyncSessionLocal.configure(bind=engine)
    yield engine
    await engine.dispose()
    os.unlink(tmp.name)


async def _seed(session_id="s1", guild="g", stype="staked", stage="completed",
                victory=None, teams=None, matches=(), start=None):
    """teams: (team_a_list, team_b_list) or None (legacy-style).
    matches: iterable of (p1, p2, winner, submitted_at_or_None)."""
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(
            session_id=session_id, guild_id=guild, session_type=stype,
            session_stage=stage,
            victory_message_id_results_channel=victory,
            team_a=list(teams[0]) if teams else None,
            team_b=list(teams[1]) if teams else None,
            draft_start_time=start or datetime(2026, 1, 1),
            cube="TestCube"))
        for i, (p1, p2, w, ts) in enumerate(matches):
            s.add(MatchResult(session_id=session_id, match_number=i + 1,
                              player1_id=p1, player2_id=p2, winner_id=w,
                              result_submitted_at=ts))
        await s.commit()


@pytest.mark.asyncio
async def test_records_basic_team_session(test_db):
    from services.ledger_stats import fetch_session_records
    await _seed(teams=(["1", "2", "3"], ["4", "5", "6"]), matches=[
        ("1", "4", "1", None), ("2", "5", "5", None), ("3", "6", "3", None),
        ("1", "5", "1", None), ("2", "6", "2", None), ("3", "4", "3", None),
        ("1", "6", "1", None), ("2", "4", "4", None), ("3", "5", "3", None),
    ])
    records = await fetch_session_records("g", player_id="1")
    assert len(records) == 1
    r = records[0]
    assert (r["wins"], r["losses"], r["matches"]) == (3, 0, 3)
    assert r["completed"] is True
    assert r["opponents"]["4"] == [1, 0]
    # team A won 7 of 9 session matches (winners: 1,5,3,1,2,3,1,4,3 -> A,B,A,A,A,A,A,B,A)
    assert (r["side_wins"], r["side_losses"]) == (7, 2)
    assert r["cube"] == "TestCube"
    assert r["session_type"] == "staked"
    assert r["participants"] == {"1", "2", "3", "4", "5", "6"}


@pytest.mark.asyncio
async def test_legacy_session_sides_from_match_positions(test_db):
    from services.ledger_stats import fetch_session_records
    # No teams JSON (legacy import): player1s are one side, player2s the other.
    await _seed(session_id="legacy-9", stage=None, teams=None, matches=[
        ("1", "4", "1", None), ("2", "5", "5", None),
        ("1", "5", "5", None), ("2", "4", "2", None),
    ])
    r = next(r for r in await fetch_session_records("g", player_id="1"))
    assert r["completed"] is True           # legacy- prefix counts as completed
    assert (r["side_wins"], r["side_losses"]) == (2, 2)


@pytest.mark.asyncio
async def test_guards_and_scope(test_db):
    from services.ledger_stats import fetch_session_records
    await _seed(session_id="sw", stype="swiss", matches=[("1", "2", "1", None)])
    await _seed(session_id="ok", stype="premade", victory="123", matches=[
        ("1", "2", "1", None),        # counts (premade IS rated)
        ("1", "1", "1", None),        # self-match: skipped
        ("1", "3", "9", None),        # winner not a participant: skipped
        ("1", "4", None, None),       # unreported: skipped
    ])
    records = await fetch_session_records("g", player_id="1")
    assert [r["session_id"] for r in records] == ["ok"]
    assert records[0]["matches"] == 1


@pytest.mark.asyncio
async def test_incomplete_native_session_flagged(test_db):
    from services.ledger_stats import fetch_session_records
    await _seed(session_id="mid", stage="pairings", victory=None,
                teams=(["1"], ["2"]), matches=[("1", "2", "1", None)])
    r = (await fetch_session_records("g", player_id="1"))[0]
    assert r["completed"] is False
    assert r["wins"] == 1                   # match still counts as a match


@pytest.mark.asyncio
async def test_since_filters_by_match_event_time(test_db):
    from services.ledger_stats import fetch_session_records
    await _seed(session_id="old", victory="1", start=datetime(2024, 5, 1),
                teams=(["1"], ["2"]),
                matches=[("1", "2", "1", None)])
    await _seed(session_id="new", victory="1", start=datetime(2026, 6, 1),
                teams=(["1"], ["2"]),
                matches=[("1", "2", "2", datetime(2026, 6, 1, 12))])
    recent = await fetch_session_records("g", player_id="1",
                                         since=datetime(2026, 1, 1))
    assert [r["session_id"] for r in recent] == ["new"]
    lifetime = await fetch_session_records("g", player_id="1")
    assert len(lifetime) == 2


@pytest.mark.asyncio
async def test_substitute_side_inferred_from_opponents(test_db):
    """A player absent from team_a/team_b (e.g. subbed in via /add_sub after
    the team JSON was written) must not be dropped from side accounting."""
    from services.ledger_stats import _side_map, fetch_session_records

    await _seed(session_id="sub", victory="1", teams=(["1", "2"], ["4", "5"]),
                matches=[("1", "4", "1", None), ("7", "4", "7", None)])

    class _M:
        def __init__(self, p1, p2):
            self.player1_id, self.player2_id = p1, p2

    session_row = await DraftSession.get_by_session_id("sub")
    sides = _side_map(session_row, [_M("1", "4"), _M("7", "4")])
    assert sides["7"] == "a"          # inferred: opposite of 4's side ('b')

    records = await fetch_session_records("g")
    by_pid = {r["player_id"]: r for r in records}

    # team A (1, 2, and inferred substitute 7) won both of the session's
    # 2 matches -- the substitute's match must count toward the session
    # side totals, not just their own personal wins/losses.
    assert (by_pid["7"]["side_wins"], by_pid["7"]["side_losses"]) == (2, 0)
    assert (by_pid["1"]["side_wins"], by_pid["1"]["side_losses"]) == (2, 0)
    assert (by_pid["4"]["side_wins"], by_pid["4"]["side_losses"]) == (0, 2)


# 4v4, 3 rounds -- NOT a full round-robin (that would take 4 rounds), so
# each team_a player faces only 3 of team_b's 4 players and vice versa. This
# is the guild's majority format and the exact shape of the "never faced =>
# teammate" bug: player "1" never plays "8" purely because the schedule
# didn't pair them this time, even though they're on opposing teams. Team A
# wins every match, so side outcome is unambiguous.
_4V4_TEAMS = (["1", "2", "3", "4"], ["5", "6", "7", "8"])
_4V4_MATCHES = [
    ("1", "5", "1", None), ("2", "6", "2", None), ("3", "7", "3", None), ("4", "8", "4", None),
    ("1", "6", "1", None), ("2", "7", "2", None), ("3", "8", "3", None), ("4", "5", "4", None),
    ("1", "7", "1", None), ("2", "8", "2", None), ("3", "5", "3", None), ("4", "6", "4", None),
]


@pytest.mark.asyncio
async def test_never_faced_opponent_not_misclassified_as_teammate_4v4(test_db):
    """The critical fix, at the fold + h2h_totals level: an opposing player
    you never happened to be paired against must not read as a teammate,
    while a real teammate (same side, also never played directly) still
    does."""
    from services.ledger_stats import fetch_session_records, h2h_totals

    await _seed(session_id="4v4", victory="v", teams=_4V4_TEAMS,
                matches=_4V4_MATCHES)
    records = await fetch_session_records("g", player_id="1")
    r = records[0]

    assert "8" not in r["opponents"]           # sanity: they truly never played
    assert "8" not in r["teammates"]           # the fix: opposing side, not a teammate
    assert {"2", "3", "4"} <= r["teammates"]   # real teammates still classified correctly

    h_never_faced = h2h_totals(records, "8")
    assert h_never_faced["drafts_with"] == 0
    assert h_never_faced["drafts_against"] == 1
    assert h_never_faced["drafts_against_won"] == 1   # side_wins (12) > side_losses (0)

    h_teammate = h2h_totals(records, "2")
    assert h_teammate["drafts_with"] == 1
    assert h_teammate["drafts_against"] == 0
    assert h_teammate["drafts_with_won"] == 1


@pytest.mark.asyncio
async def test_leaderboard_teammate_stats_4v4_never_faced_not_teammate(test_db, monkeypatch):
    """Same 4v4 fixture through get_leaderboard_data's time_vault_and_key
    (teammate/Vault-and-Key) pass -- the never-faced opposing player must
    not surface as a partnership, and a real teammate must."""
    from services import leaderboard_service

    # A single session can't clear the real partnership-drafts minimums
    # (3-8 depending on timeframe); lower it so this session alone is
    # enough to exercise the with/against classification.
    monkeypatch.setattr(
        leaderboard_service, "get_minimum_requirements",
        lambda timeframe: {"drafts": 0, "matches": 0, "partnership_drafts": 1})

    await _seed(session_id="4v4", victory="v", teams=_4V4_TEAMS,
                matches=_4V4_MATCHES)
    data = await leaderboard_service.get_leaderboard_data(
        "g", category="time_vault_and_key", timeframe="lifetime")
    pairs = {frozenset((p["player_id"], p["teammate_id"])) for p in data}

    assert frozenset(("1", "8")) not in pairs   # never faced, opposing side -- not a partnership
    assert frozenset(("1", "2")) in pairs       # real teammates, never faced directly either


def _rec(**kw):
    """teammates defaults to the old opponents-absence heuristic (fine for
    these side-less synthetic records); pass teammates=... explicitly to
    model a record where side, not opponents-absence, decides it."""
    base = dict(player_id="1", session_id="s", session_type="staked",
                cube="CubeA", completed=True, started_at=None,
                wins=0, losses=0, matches=0, opponents={},
                side_wins=0, side_losses=0, participants=set(),
                teammates=None)
    base.update(kw)
    base["matches"] = base["wins"] + base["losses"]
    if base["teammates"] is None:
        base["teammates"] = (
            base["participants"] - {base["player_id"]} - set(base["opponents"].keys()))
    return base


def test_projections_match_draft_trophy_team():
    from services.ledger_stats import (
        match_totals, draft_totals, trophy_count, team_record)
    records = [
        _rec(session_id="a", wins=3, losses=0, side_wins=6, side_losses=3),
        _rec(session_id="b", wins=2, losses=1, side_wins=4, side_losses=5),
        _rec(session_id="c", wins=2, losses=0, completed=False,
             side_wins=2, side_losses=0),          # in-progress
        _rec(session_id="d", wins=1, losses=1, side_wins=4, side_losses=4),
    ]
    assert match_totals(records) == {"matches_played": 10, "matches_won": 8}
    assert draft_totals(records) == 3                 # c not completed
    assert trophy_count(records) == 1                 # only a (3-0, completed)
    assert team_record(records) == {"played": 3, "won": 1, "lost": 1, "tied": 1}


def test_projection_cube_and_h2h():
    from services.ledger_stats import cube_breakdown, h2h_totals
    records = [
        _rec(session_id="a", cube="X", wins=2, losses=1,
             opponents={"9": [1, 1], "8": [1, 0]}, participants={"1", "9", "8"}),
        _rec(session_id="b", cube=None, wins=1, losses=0,
             opponents={"7": [1, 0]}, participants={"1", "7"}),               # 9 not an opponent here
    ]
    cubes = cube_breakdown(records)
    assert cubes["X"] == {"wins": 2, "losses": 1, "drafts": 1}
    assert cubes["Unknown"] == {"wins": 1, "losses": 0, "drafts": 1}
    h = h2h_totals(records, "9")
    assert h["matches_played"] == 2 and h["matches_won"] == 1
    assert h["drafts_against"] == 1                   # session a: they met
    assert h["drafts_with"] == 0                      # session b: 9 absent entirely


def test_cube_breakdown_groups_case_insensitively_completed_only():
    from services.ledger_stats import cube_breakdown
    records = [
        _rec(session_id="a", cube="LSVCube", wins=2, losses=1),
        _rec(session_id="b", cube="Lsvcube", wins=1, losses=0),
        _rec(session_id="c", cube="lsvcube", wins=1, losses=1),
        # in-progress: wins/losses still fold in, but shouldn't add to drafts
        _rec(session_id="d", cube="Lsvcube", wins=0, losses=1, completed=False),
        _rec(session_id="e", cube="OtherCube", wins=1, losses=0),
    ]
    cubes = cube_breakdown(records)
    # 3 differently-cased spellings collapse into one entry, displayed under
    # "Lsvcube" -- the most common spelling (b and d), tie-broken by recency.
    assert set(cubes.keys()) == {"Lsvcube", "OtherCube"}
    lsv = cubes["Lsvcube"]
    assert (lsv["wins"], lsv["losses"]) == (4, 3)
    assert lsv["drafts"] == 3          # d excluded: not completed


def test_h2h_totals_teammate_and_opponent_win_counters():
    from services.ledger_stats import h2h_totals
    records = [
        # "9" is in participants but never faced directly (not in
        # opponents) -> teammates; side_wins > side_losses -> a win.
        _rec(session_id="a", wins=1, losses=0, opponents={"7": [1, 0]},
             participants={"1", "9", "7"}, side_wins=5, side_losses=2),
        # "9" met across the table (present in opponents) with a side win.
        _rec(session_id="b", wins=1, losses=0, opponents={"9": [1, 0]},
             participants={"1", "9"}, side_wins=3, side_losses=1),
        # teammates, tied session (side_wins == side_losses).
        _rec(session_id="c", wins=1, losses=1, opponents={"7": [1, 1]},
             participants={"1", "9", "7"}, side_wins=4, side_losses=4),
        # met across the table, tied session.
        _rec(session_id="d", wins=1, losses=1, opponents={"9": [1, 1]},
             participants={"1", "9"}, side_wins=2, side_losses=2),
    ]
    h = h2h_totals(records, "9")
    assert h["drafts_with"] == 2
    assert h["drafts_with_won"] == 1
    assert h["drafts_with_tied"] == 1
    assert h["drafts_against"] == 2
    assert h["drafts_against_won"] == 1
    assert h["drafts_against_tied"] == 1


@pytest.mark.asyncio
async def test_h2h_from_ledger_scopes_and_counts(test_db):
    from player_stats import get_head_to_head_stats
    await _seed(session_id="sw", stype="swiss", matches=[("1", "9", "9", None)])
    await _seed(session_id="a", victory="v", teams=(["1", "2"], ["9", "8"]),
                matches=[("1", "9", "1", None), ("2", "8", "8", None),
                         ("1", "8", "1", None), ("2", "9", "2", None)])
    # Teammates ("1" and "9" never face each other directly here) whose
    # side ties 1-1 -- must land as a draw, not a loss, in the dict
    # create_head_to_head_embed reads.
    await _seed(session_id="tie", victory="v2", teams=(["1", "9"], ["8", "7"]),
                matches=[("1", "8", "1", None), ("9", "7", "7", None)])
    h = await get_head_to_head_stats("1", "9", "One", "Nine", "g")
    # swiss excluded: direct record is 1-0, not 1-1
    assert h["lifetime"]["matches_played"] == 1
    assert h["lifetime"]["user1_wins"] == 1
    assert h["lifetime"]["user2_wins"] == 0
    # tied team session shows up as a draw, not a loss
    assert h["teammate_lifetime"]["draws"] == 1
    assert h["teammate_lifetime"]["wins"] == 0
    assert h["teammate_lifetime"]["losses"] == 0
    # they met across the table in session "a" and user1's side won it
    assert h["opposing_lifetime"]["wins"] == 1
    assert h["opposing_lifetime"]["losses"] == 0


@pytest.mark.asyncio
async def test_get_player_statistics_counts_from_ledger(test_db):
    from player_stats import get_player_statistics
    # A completed 3v3 the player 3-0s, plus a legacy session, plus a premade.
    await _seed(session_id="native", victory="v1",
                teams=(["1", "2", "3"], ["4", "5", "6"]), matches=[
        ("1", "4", "1", None), ("2", "5", "5", None), ("3", "6", "3", None),
        ("1", "5", "1", None), ("2", "6", "2", None), ("3", "4", "3", None),
        ("1", "6", "1", None), ("2", "4", "4", None), ("3", "5", "3", None)])
    await _seed(session_id="legacy-1", stage=None, teams=None,
                matches=[("1", "9", "1", None), ("1", "9", "9", None)])
    await _seed(session_id="pm", stype="premade", victory="v2",
                teams=(["1"], ["9"]), matches=[("1", "9", "1", None)])

    stats = await get_player_statistics("1", None, "One", "g")
    assert stats["matches_played"] == 6      # 3 + 2 + 1
    assert stats["matches_won"] == 5
    assert stats["drafts_played"] == 3
    assert stats["trophies_won"] == 1        # only the native 3-0
    assert stats["team_drafts_played"] == 3
    # cube_stats carries only what the embed reads: draft count + win %
    cube = stats["cube_stats"]["TestCube"]
    assert cube["drafts_played"] == 3
    assert round(cube["win_percentage"], 1) == round(5 / 6 * 100, 1)


@pytest.mark.asyncio
async def test_leaderboard_counts_premade_and_legacy(test_db):
    """Today's query filters session_type IN ('random', 'staked') AND
    victory_message_id_results_channel IS NOT NULL -- a legacy session (no
    victory message) and a premade session (wrong type) are both invisible.
    drafts_played has no minimum-games gate, so it isolates the visibility
    bug directly; the same player_data entry still carries the match-total
    fields so we can check those weren't lost either."""
    from services.leaderboard_service import get_leaderboard_data
    await _seed(session_id="legacy-2", stage=None, teams=None,
                matches=[("1", "9", "1", None), ("1", "9", "1", None),
                         ("1", "9", "1", None)])
    await _seed(session_id="pm", stype="premade", victory="v",
                teams=(["1"], ["9"]), matches=[("1", "9", "1", None)])
    data = await get_leaderboard_data("g", category="drafts_played",
                                       limit=5, timeframe="lifetime")
    top = next(p for p in data if p["player_id"] == "1")
    assert top["drafts_played"] == 2         # legacy-2 + pm
    assert top["matches_won"] == 4
    assert top["completed_matches"] == 4
