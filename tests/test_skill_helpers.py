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


def test_skill_rating_scales_conservative_estimate():
    # (25 - 3*8.333) * 40 = (25 - 24.999) * 40 ≈ 0.04 -> rounds to 0
    assert skill_rating(25.0, 25.0 / 3) == 0
    # A converged strong player: (30 - 3*1) * 40 = 1080
    assert skill_rating(30.0, 1.0) == 1080


def test_is_established_threshold():
    assert is_established(9) is False
    assert is_established(10) is True
    assert is_established(11) is True


def test_new_ratings_moves_winner_up_loser_down_and_shrinks_sigma():
    nw_mu, nw_sig, nl_mu, nl_sig = new_ratings(25.0, 25.0 / 3, 25.0, 25.0 / 3)
    assert nw_mu > 25.0
    assert nl_mu < 25.0
    assert nw_sig < 25.0 / 3
    assert nl_sig < 25.0 / 3
