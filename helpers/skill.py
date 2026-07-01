"""Shared TrueSkill environment and rating helpers.

The bot rates players with the official ``trueskill`` library. This module owns
the single environment (draw probability 0 — these matches never draw) plus the
small pure helpers used by the live update path (utils.py), the session-type
guard (views.py), the display slices, and the backfill migration. Keeping the
environment and the backfill in one place guarantees live and historical values
are computed identically.

Only depends on ``trueskill`` and ``sqlalchemy`` so it is safe to import from an
Alembic migration (no app-model imports).
"""
from trueskill import TrueSkill

# All library defaults except draw_probability: mu0=25, sigma0=25/3, beta=25/6,
# tau=25/300. These match the player_stats column defaults.
SKILL_ENV = TrueSkill(draw_probability=0.0)

PRIOR_MU = 25.0
PRIOR_SIGMA = 25.0 / 3

# Draft types whose match results move the skill rating. Swiss is excluded.
RATING_SESSION_TYPES = ("random", "staked", "premade")


def rating_counts_for(session_type):
    """True iff a draft of this session type should update skill ratings."""
    return session_type in RATING_SESSION_TYPES


def skill_rating(mu, sigma):
    """Scaled, Elo-like conservative rating for display: round((mu - 3*sigma) * 40)."""
    return round((mu - 3 * sigma) * 40)


def is_established(drafts):
    """True once a player has enough drafts to leave 'provisional' status."""
    return drafts >= 10


def new_ratings(winner_mu, winner_sigma, loser_mu, loser_sigma):
    """One 1v1 update through the shared environment. Returns
    (new_winner_mu, new_winner_sigma, new_loser_mu, new_loser_sigma)."""
    winner = SKILL_ENV.create_rating(mu=winner_mu, sigma=winner_sigma)
    loser = SKILL_ENV.create_rating(mu=loser_mu, sigma=loser_sigma)
    new_winner, new_loser = SKILL_ENV.rate_1vs1(winner, loser)
    return new_winner.mu, new_winner.sigma, new_loser.mu, new_loser.sigma
