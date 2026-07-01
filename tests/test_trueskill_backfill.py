"""Backfill recompute logic (helpers.skill.backfill_skill_ratings) on a temp DB."""
from sqlalchemy import create_engine, text

from helpers.skill import PRIOR_MU, PRIOR_SIGMA, backfill_skill_ratings, new_ratings

DDL = [
    """CREATE TABLE player_stats (
        player_id TEXT, guild_id TEXT, display_name TEXT,
        true_skill_mu REAL, true_skill_sigma REAL,
        games_won INTEGER, games_lost INTEGER,
        PRIMARY KEY (player_id, guild_id))""",
    """CREATE TABLE draft_sessions (
        session_id TEXT, guild_id TEXT, session_type TEXT, draft_start_time TEXT)""",
    """CREATE TABLE match_results (
        id INTEGER PRIMARY KEY, session_id TEXT, player1_id TEXT, player2_id TEXT,
        winner_id TEXT, result_submitted_at TEXT)""",
]


def _conn():
    engine = create_engine("sqlite://")  # in-memory, single connection
    conn = engine.connect()
    for stmt in DDL:
        conn.execute(text(stmt))
    return conn


def _add_player(conn, pid, guild="g1"):
    conn.execute(text(
        "INSERT INTO player_stats (player_id, guild_id, display_name, true_skill_mu, "
        "true_skill_sigma, games_won, games_lost) VALUES (:p, :g, :p, 99.0, 99.0, 7, 7)"),
        {"p": pid, "g": guild})


def _add_session(conn, sid, stype, start, guild="g1"):
    conn.execute(text(
        "INSERT INTO draft_sessions (session_id, guild_id, session_type, draft_start_time) "
        "VALUES (:s, :g, :t, :d)"), {"s": sid, "g": guild, "t": stype, "d": start})


def _add_match(conn, mid, sid, p1, p2, winner, submitted):
    conn.execute(text(
        "INSERT INTO match_results (id, session_id, player1_id, player2_id, winner_id, "
        "result_submitted_at) VALUES (:i, :s, :a, :b, :w, :t)"),
        {"i": mid, "s": sid, "a": p1, "b": p2, "w": winner, "t": submitted})


def _rating(conn, pid, guild="g1"):
    row = conn.execute(text(
        "SELECT true_skill_mu, true_skill_sigma, games_won, games_lost FROM player_stats "
        "WHERE player_id=:p AND guild_id=:g"), {"p": pid, "g": guild}).fetchone()
    return row


def test_single_premade_match_matches_new_ratings():
    conn = _conn()
    _add_player(conn, "1"); _add_player(conn, "2")
    _add_session(conn, "s1", "premade", "2026-01-01")
    _add_match(conn, 1, "s1", "1", "2", "1", "2026-01-01T10:00:00")

    backfill_skill_ratings(conn)

    exp_w_mu, exp_w_sig, exp_l_mu, exp_l_sig = new_ratings(PRIOR_MU, PRIOR_SIGMA, PRIOR_MU, PRIOR_SIGMA)
    w = _rating(conn, "1"); l = _rating(conn, "2")
    assert round(w[0], 6) == round(exp_w_mu, 6)
    assert round(w[1], 6) == round(exp_w_sig, 6)
    assert (w[2], w[3]) == (1, 0)   # winner: 1 game won, 0 lost
    assert round(l[0], 6) == round(exp_l_mu, 6)
    assert round(l[1], 6) == round(exp_l_sig, 6)
    assert (l[2], l[3]) == (0, 1)   # loser: 0 won, 1 lost


def test_reset_wipes_prior_values_for_unrated_players():
    conn = _conn()
    _add_player(conn, "99")  # no matches
    backfill_skill_ratings(conn)
    mu, sig, gw, gl = _rating(conn, "99")
    assert mu == PRIOR_MU and round(sig, 3) == round(PRIOR_SIGMA, 3)
    assert (gw, gl) == (0, 0)


def test_swiss_and_test_users_excluded():
    conn = _conn()
    _add_player(conn, "1"); _add_player(conn, "2")
    big = str(900000000000000000 + 5)
    _add_player(conn, big)
    _add_session(conn, "sw", "swiss", "2026-01-01")
    _add_match(conn, 1, "sw", "1", "2", "1", "2026-01-01T10:00:00")   # swiss: ignored
    _add_session(conn, "sp", "premade", "2026-01-02")
    _add_match(conn, 2, "sp", "1", big, "1", "2026-01-02T10:00:00")   # test user: ignored

    backfill_skill_ratings(conn)

    # Player 1 played only ignored matches -> stays at the prior with 0 games.
    mu, sig, gw, gl = _rating(conn, "1")
    assert mu == PRIOR_MU and (gw, gl) == (0, 0)


def test_premade_player_without_row_gets_inserted():
    conn = _conn()
    _add_player(conn, "1")            # has a row
    # player "2" intentionally has NO player_stats row
    _add_session(conn, "sp", "premade", "2026-01-01")
    _add_match(conn, 1, "sp", "1", "2", "1", "2026-01-01T10:00:00")

    backfill_skill_ratings(conn)

    loser = _rating(conn, "2")        # previously would be None (dropped)
    assert loser is not None
    assert (loser[2], loser[3]) == (0, 1)          # 0 games won, 1 lost
    assert loser[0] < PRIOR_MU                       # loser mu fell below prior


def test_chronology_uses_submitted_then_start_time_and_counts_games():
    conn = _conn()
    _add_player(conn, "1"); _add_player(conn, "2")
    _add_session(conn, "s1", "staked", "2026-01-01")
    _add_session(conn, "s2", "random", "2026-01-02")
    # Two matches, player 1 wins both -> games (2-0) for p1, (0-2) for p2, mu(1) rises.
    _add_match(conn, 1, "s1", "1", "2", "1", "2026-01-01T10:00:00")
    _add_match(conn, 2, "s2", "2", "1", "1", "2026-01-02T10:00:00")
    backfill_skill_ratings(conn)
    w = _rating(conn, "1"); l = _rating(conn, "2")
    assert (w[2], w[3]) == (2, 0)
    assert (l[2], l[3]) == (0, 2)
    assert w[0] > PRIOR_MU and l[0] < PRIOR_MU
