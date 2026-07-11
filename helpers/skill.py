"""Shared TrueSkill environment and rating helpers.

The bot rates players with the official ``trueskill`` library. This module owns
the single environment (draw probability 0 — these matches never draw) plus the
small pure helpers used by the live update path (utils.py), the session-type
guard (views.py), the display slices, and the backfill migration. Keeping the
environment and the backfill in one place guarantees live and historical values
are computed identically. The backfill also skips TEST_MODE users; the live
path does not, which only matters under TEST_MODE.

Only depends on ``trueskill`` and ``sqlalchemy`` so it is safe to import from an
Alembic migration (no app-model imports).
"""
from collections import defaultdict

from sqlalchemy import text
from trueskill import TrueSkill

from helpers.test_users import TEST_USER_ID_BASE

# All library defaults except draw_probability: mu0=25, sigma0=25/3, beta=25/6,
# tau=25/300. These match the player_stats column defaults.
SKILL_ENV = TrueSkill(draw_probability=0.0)

PRIOR_MU = 25.0
PRIOR_SIGMA = 25.0 / 3

# Draft types whose match results move the skill rating. Swiss is excluded.
RATING_SESSION_TYPES = ("random", "staked", "premade")

# Rated games (random+staked+premade, games_won+games_lost) needed before a
# player's rating is shown without the "(provisional)" flag.
ESTABLISHED_GAMES = 20


def _is_test_user(player_id):
    """True for synthetic TEST_MODE users. Non-numeric ids (legacy/imported) are
    treated as real, never crashing the backfill."""
    pid = str(player_id)
    return pid.isdigit() and int(pid) >= TEST_USER_ID_BASE


def rating_counts_for(session_type):
    """True iff a draft of this session type should update skill ratings."""
    return session_type in RATING_SESSION_TYPES


def skill_rating(mu, sigma):
    """Scaled, Elo-like conservative rating for display: round((mu - 3*sigma) * 40)."""
    return round((mu - 3 * sigma) * 40)


def is_established(games):
    """True once a player has enough rated games (incl. premade) to shed the
    provisional label. ~20 games ≈ the repo's mid-tier match minimum and the
    original ~10-draft intent."""
    return games >= ESTABLISHED_GAMES


def new_ratings(winner_mu, winner_sigma, loser_mu, loser_sigma):
    """One 1v1 update through the shared environment. Returns
    (new_winner_mu, new_winner_sigma, new_loser_mu, new_loser_sigma)."""
    winner = SKILL_ENV.create_rating(mu=winner_mu, sigma=winner_sigma)
    loser = SKILL_ENV.create_rating(mu=loser_mu, sigma=loser_sigma)
    new_winner, new_loser = SKILL_ENV.rate_1vs1(winner, loser)
    return new_winner.mu, new_winner.sigma, new_loser.mu, new_loser.sigma


def backfill_skill_ratings(connection):
    """Recompute μ/σ and games-won/lost for every player from scratch.

    Resets all player_stats to the prior with zero rating-games, then replays all
    random/staked/premade 1v1 results chronologically per guild (excluding test
    users, self-matches, and rows whose winner is not one of the two players) and
    writes the final values back. Streaks, drafts_participated, and elo_rating are
    left untouched. Takes a SQLAlchemy Connection so it works from an Alembic
    migration (op.get_bind()) and from tests.
    """
    connection.execute(
        text("UPDATE player_stats SET true_skill_mu = :mu, true_skill_sigma = :sig, "
             "games_won = 0, games_lost = 0"),
        {"mu": PRIOR_MU, "sig": PRIOR_SIGMA},
    )

    rows = connection.execute(text(
        "SELECT m.player1_id, m.player2_id, m.winner_id, d.guild_id "
        "FROM match_results m JOIN draft_sessions d ON m.session_id = d.session_id "
        "WHERE d.session_type IN ('random', 'staked', 'premade') "
        "AND m.winner_id IS NOT NULL "
        "ORDER BY COALESCE(m.result_submitted_at, d.draft_start_time), m.id"
    )).fetchall()

    mu = defaultdict(lambda: PRIOR_MU)
    sigma = defaultdict(lambda: PRIOR_SIGMA)
    games_won = defaultdict(int)
    games_lost = defaultdict(int)

    for player1_id, player2_id, winner_id, guild_id in rows:
        if not player1_id or not player2_id or player1_id == player2_id:
            continue
        if winner_id not in (player1_id, player2_id):
            continue
        if _is_test_user(player1_id) or _is_test_user(player2_id):
            continue
        loser_id = player2_id if winner_id == player1_id else player1_id
        kw = (guild_id, winner_id)
        kl = (guild_id, loser_id)
        new_w_mu, new_w_sig, new_l_mu, new_l_sig = new_ratings(
            mu[kw], sigma[kw], mu[kl], sigma[kl]
        )
        mu[kw], sigma[kw] = new_w_mu, new_w_sig
        mu[kl], sigma[kl] = new_l_mu, new_l_sig
        games_won[kw] += 1
        games_lost[kl] += 1

    # Every games_won/games_lost key is also a mu key (both players in each
    # rated match get a mu entry), so iterating mu covers every touched player.
    for key in mu:
        guild_id, player_id = key
        # ON CONFLICT upsert assumes SQLite/Postgres syntax (the repo's SQLite).
        connection.execute(
            text(
                "INSERT INTO player_stats "
                "(player_id, guild_id, true_skill_mu, true_skill_sigma, games_won, games_lost) "
                "VALUES (:p, :g, :mu, :sig, :gw, :gl) "
                "ON CONFLICT(player_id, guild_id) DO UPDATE SET "
                "true_skill_mu = excluded.true_skill_mu, "
                "true_skill_sigma = excluded.true_skill_sigma, "
                "games_won = excluded.games_won, "
                "games_lost = excluded.games_lost"
            ),
            {"mu": mu[key], "sig": sigma[key], "gw": games_won[key],
             "gl": games_lost[key], "g": guild_id, "p": player_id},
        )
