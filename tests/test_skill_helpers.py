"""Unit tests for the shared TrueSkill helpers (helpers/skill.py)."""
from helpers.skill import (
    PRIOR_MU,
    PRIOR_SIGMA,
    SKILL_ENV,
    is_established,
    new_ratings,
    rating_counts_for,
    skill_rating,
)


def test_env_has_zero_draw_probability():
    assert SKILL_ENV.draw_probability == 0.0


def test_priors_match_column_defaults():
    assert PRIOR_MU == 25.0
    assert round(PRIOR_SIGMA, 3) == 8.333


def test_rating_counts_for_included_and_excluded_types():
    assert rating_counts_for("random") is True
    assert rating_counts_for("staked") is True
    assert rating_counts_for("premade") is True
    assert rating_counts_for("swiss") is False
    assert rating_counts_for("test") is False
    assert rating_counts_for(None) is False


def test_skill_rating_is_elo_anchored():
    # A brand-new player sits exactly on the 1500 anchor regardless of sigma
    assert skill_rating(25.0, 25.0 / 3, 0) == 1500
    # A proven strong player: 1500 + (29 - 25) * (270/300) * 95 = 1842
    assert skill_rating(29.0, 1.0, 270) == 1842
    # Sigma does not move the displayed number
    assert skill_rating(29.0, 8.0, 270) == 1842
    # Below-average play reads below the anchor, shrunk the same way
    assert skill_rating(21.0, 1.0, 270) == 1158


def test_skill_rating_shrinks_small_samples():
    # A hot 20-game record cannot spike past a proven strong player: the
    # games-based shrink discounts a higher mu earned over a short sample.
    hot_newcomer = skill_rating(30.5, 1.8, 22)
    proven_strong = skill_rating(29.0, 0.8, 270)
    assert hot_newcomer < proven_strong
    # More games at the same mu always earn more distance from the anchor
    assert skill_rating(29.0, 1.0, 30) < skill_rating(29.0, 1.0, 300)
    # An average record stays at the anchor no matter the sample size
    assert skill_rating(25.0, 1.0, 500) == 1500


def test_is_established_threshold():
    assert is_established(19) is False
    assert is_established(20) is True
    assert is_established(21) is True


def test_new_ratings_moves_winner_up_loser_down_and_shrinks_sigma():
    nw_mu, nw_sig, nl_mu, nl_sig = new_ratings(25.0, 25.0 / 3, 25.0, 25.0 / 3)
    assert nw_mu > 25.0
    assert nl_mu < 25.0
    assert nw_sig < 25.0 / 3
    assert nl_sig < 25.0 / 3
