"""Shared TrueSkill environment and rating helpers.

The bot rates players with the official ``trueskill`` library. This module owns
the single environment (draw probability 0 — these matches never draw) plus the
small pure helpers used by the live update path (utils.py), the session-type
guard (views.py), the display slices, and the backfill migration. Keeping the
environment and the backfill in one place guarantees live and historical values
are computed identically: both rate every 1v1 result in rated session types,
with no player filtering (prod data contains no synthetic TEST_MODE users, and
a divergence here once silently dropped thousands of real games).

Only depends on ``trueskill`` and ``sqlalchemy`` so it is safe to import from an
Alembic migration (no app-model imports).
"""
from collections import defaultdict

from sqlalchemy import text
from trueskill import TrueSkill

from helpers.test_users import TEST_USER_ID_BASE, TEST_USER_ID_CEILING

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
    """True for synthetic TEST_MODE users: the sequential allocator band only.
    Real post-2021 Discord snowflakes are also >= TEST_USER_ID_BASE and must
    never match. Used solely for the TEST_MODE display floor
    (utils._fetch_player_stats_map) — never to filter rating updates."""
    pid = str(player_id)
    return pid.isdigit() and TEST_USER_ID_BASE <= int(pid) < TEST_USER_ID_CEILING


def rating_counts_for(session_type):
    """True iff a draft of this session type should update skill ratings."""
    return session_type in RATING_SESSION_TYPES


def rating_update_action(previous_winner_id, new_winner_id):
    """What the live path must do when a result is (re)submitted.

    'apply'     — first real report: one incremental TrueSkill update.
    'none'      — nothing rateable changed (no-play report, or a re-report
                  keeping the same winner, e.g. a 2-0 -> 2-1 score fix).
    'recompute' — the stored winner changed (flip or un-report): the old
                  update is baked into player_stats and TrueSkill updates
                  are order-dependent, so heal by replaying the ledger.
    """
    if previous_winner_id is None:
        return "apply" if new_winner_id else "none"
    if previous_winner_id == new_winner_id:
        return "none"
    return "recompute"


# Display anchoring: a new player shows exactly RATING_ANCHOR; each TrueSkill
# mu point moves the display by up to RATING_POINTS_PER_MU, discounted by a
# games-based shrink factor g/(g + RATING_SHRINK_GAMES). The shrink keeps
# short hot streaks from spiking past long proven records (a 16-6 newcomer
# earns less than half of their mu edge until ~30 games), while the anchor
# still holds exactly at zero games. 95/mu is wider than the
# win-probability-faithful Elo conversion (~45/mu) on purpose — it puts the
# server's top proven players around ~1850, matching the familiar MTG Elo
# Project scale, at the cost of overstating win odds implied by point gaps.
RATING_ANCHOR = 1500
RATING_POINTS_PER_MU = 95
RATING_SHRINK_GAMES = 30


def skill_rating(mu, sigma, games):
    """Elo-anchored display rating with small-sample shrinkage.

    round(1500 + (mu - 25) * g/(g+30) * 95): a new player shows exactly 1500,
    the strongest proven players reach ~1850, and a hot short record is pulled
    toward the anchor until it is earned over more games. Uses mu alone (not
    the mu - 3*sigma conservative floor) so the anchor holds for new players
    too; the "(provisional)" label under ESTABLISHED_GAMES flags the remaining
    uncertainty.
    """
    del sigma  # kept in the signature for call-site stability
    weight = games / (games + RATING_SHRINK_GAMES)
    return round(RATING_ANCHOR + (mu - PRIOR_MU) * weight * RATING_POINTS_PER_MU)


def is_established(games):
    """True once a player has enough rated games (incl. premade) to shed the
    provisional label. ~20 games ≈ the repo's mid-tier match minimum and the
    original ~10-draft intent."""
    return games >= ESTABLISHED_GAMES


# --- Upset victory callout -------------------------------------------------
# A winning team whose post-draft win probability (Elo logistic on average
# display ratings — the predictor that backtested well-calibrated, ECE ~1%)
# is below these thresholds gets victory-message flair. Post-draft ratings
# are deliberate (owner's criterion): if the win itself moved the ratings
# enough to erase the upset, it wasn't much of an upset. See
# docs/superpowers/specs/2026-07-31-upset-victory-callout-design.md.
UPSET_THRESHOLD = 0.35
LEGENDARY_UPSET_THRESHOLD = 0.25

# TEST_MODE only: pinned (mu, sigma, games) making synthetic test users a
# guaranteed heavy underdog (display rating ≈ -806), so any bot win exercises
# the legendary-upset path end to end without engineering real rating gaps.
# Applied in utils._fetch_player_stats_map, gated on is_test_mode().
TEST_USER_RATING_FLOOR = (0.0, PRIOR_SIGMA, 1000)


def team_win_probability(team_a_stats, team_b_stats):
    """P(team A wins) via the Elo logistic on average display ratings.

    Each argument is a non-empty list of (mu, sigma, games) tuples, one per
    player. Uses the same display conversion as the leaderboard so the odds
    match what players see there.
    """
    avg_a = sum(skill_rating(mu, sigma, g) for mu, sigma, g in team_a_stats) / len(team_a_stats)
    avg_b = sum(skill_rating(mu, sigma, g) for mu, sigma, g in team_b_stats) / len(team_b_stats)
    return 1 / (1 + 10 ** ((avg_b - avg_a) / 400))


def winner_probability_from_stats(stats_map, winner_ids, loser_ids):
    """Winning team's probability from {player_id: (mu, sigma, games)}.

    Players absent from the map (never rated in this guild) count as the
    prior — exactly how the calibration backtest treated them.
    """
    def tup(player_id):
        return stats_map.get(str(player_id), (PRIOR_MU, PRIOR_SIGMA, 0))

    return team_win_probability(
        [tup(p) for p in winner_ids], [tup(p) for p in loser_ids]
    )


def upset_tier(winner_prob):
    """'legendary' below 25%, 'upset' below 35%, else None. Strict <."""
    if winner_prob < LEGENDARY_UPSET_THRESHOLD:
        return "legendary"
    if winner_prob < UPSET_THRESHOLD:
        return "upset"
    return None


def underdog_odds_text(winner_prob):
    """Gambling-style odds for a winning underdog, e.g. 0.25 -> '~3:1'."""
    return f"~{round((1 - winner_prob) / winner_prob)}:1"


DISCORD_TITLE_LIMIT = 256


def apply_upset_decoration(title, description, winner_prob):
    """Winner-framed victory flair. Returns (title, description) unchanged
    when the win wasn't an upset. Loser names must never appear here.

    If the flair prefix would push the title past Discord's 256-char embed
    title limit, the original title is kept as-is (Discord 400s on oversized
    titles, which would otherwise take down the victory post entirely); the
    description flair line is always added since the 4096-char description
    limit is never at risk here.
    """
    tier = upset_tier(winner_prob)
    if tier == "upset":
        flaired_title = f"🚨 UPSET VICTORY — {title}"
        return (
            title if len(flaired_title) > DISCORD_TITLE_LIMIT else flaired_title,
            f"{description}\nThey won as {underdog_odds_text(winner_prob)} underdogs!",
        )
    if tier == "legendary":
        flaired_title = f"🌟 LEGENDARY UPSET — {title}"
        return (
            title if len(flaired_title) > DISCORD_TITLE_LIMIT else flaired_title,
            f"{description}\nThey defied {underdog_odds_text(winner_prob)} odds "
            "— one of the rarest results this server produces!",
        )
    return title, description


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
    random/staked/premade 1v1 results chronologically per guild (excluding
    self-matches and rows whose winner is not one of the two players) and
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
