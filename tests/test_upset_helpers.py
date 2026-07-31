"""Pure upset-callout math and copy: probability, tiers, odds text, decoration."""
import pytest

from helpers.skill import (
    LEGENDARY_UPSET_THRESHOLD,
    PRIOR_MU,
    PRIOR_SIGMA,
    UPSET_THRESHOLD,
    apply_upset_decoration,
    team_win_probability,
    underdog_odds_text,
    upset_tier,
    winner_probability_from_stats,
)

PRIOR = (PRIOR_MU, PRIOR_SIGMA, 0)          # displays as exactly 1500
STRONG = (30.0, 1.0, 30)                    # (30-25)*0.5*95 = +237.5 -> display 1738


def test_equal_teams_are_a_coin_flip():
    assert team_win_probability([PRIOR] * 3, [PRIOR] * 3) == pytest.approx(0.5)


def test_probabilities_are_complementary():
    p_a = team_win_probability([STRONG] * 3, [PRIOR] * 3)
    p_b = team_win_probability([PRIOR] * 3, [STRONG] * 3)
    assert p_a + p_b == pytest.approx(1.0)


def test_known_gap_matches_elo_logistic():
    # displays 1738 vs 1500 -> gap 238 -> 1/(1+10^(-238/400)) ~= 0.7974
    p = team_win_probability([STRONG] * 3, [PRIOR] * 3)
    assert p == pytest.approx(0.7974, abs=1e-3)


def test_thresholds_have_expected_values():
    assert UPSET_THRESHOLD == 0.35
    assert LEGENDARY_UPSET_THRESHOLD == 0.25


def test_upset_tier_boundaries():
    assert upset_tier(0.349) == "upset"
    assert upset_tier(0.35) is None
    assert upset_tier(0.249) == "legendary"
    assert upset_tier(0.25) == "upset"      # strict <, so exactly 0.25 is the lower tier's edge
    assert upset_tier(0.5) is None


def test_underdog_odds_text():
    assert underdog_odds_text(0.25) == "~3:1"
    assert underdog_odds_text(0.34) == "~2:1"
    assert underdog_odds_text(0.15) == "~6:1"


def test_winner_probability_missing_players_fall_back_to_prior():
    # Empty map -> both teams all-prior -> 0.5
    assert winner_probability_from_stats({}, ["1", "2", "3"], ["4", "5", "6"]) == pytest.approx(0.5)


def test_winner_probability_reads_map_by_string_id():
    stats_map = {"4": STRONG, "5": STRONG, "6": STRONG}
    p = winner_probability_from_stats(stats_map, ["1", "2", "3"], ["4", "5", "6"])
    assert p == pytest.approx(1 - 0.7974, abs=1e-3)


def test_apply_upset_decoration_upset_tier():
    title, desc = apply_upset_decoration("Congratulations to A, B, C on winning the draft!", "Draft Start: X", 0.30)
    assert title.startswith("🚨 UPSET VICTORY — Congratulations")
    assert "They won as ~2:1 underdogs!" in desc
    assert desc.startswith("Draft Start: X")


def test_apply_upset_decoration_legendary_tier():
    title, desc = apply_upset_decoration("Congratulations!", "D", 0.20)
    assert title.startswith("🌟 LEGENDARY UPSET — ")
    assert "They defied ~4:1 odds — one of the rarest results this server produces!" in desc


def test_apply_upset_decoration_no_tier_is_passthrough():
    assert apply_upset_decoration("T", "D", 0.60) == ("T", "D")
    assert apply_upset_decoration("T", "D", 0.35) == ("T", "D")


def test_apply_upset_decoration_long_title_skips_prefix_to_respect_discord_limit():
    # 250-char title + either flair prefix would exceed Discord's 256-char title
    # limit, so the title must pass through unchanged while the description
    # (4096-char limit, always safe) still gets the flair line.
    long_title = "A" * 250
    title, desc = apply_upset_decoration(long_title, "Draft Start: X", 0.30)
    assert title == long_title
    assert "They won as ~2:1 underdogs!" in desc
    assert desc.startswith("Draft Start: X")


def test_apply_upset_decoration_normal_title_still_gets_prefix():
    # Sanity check the guard doesn't affect ordinary short titles.
    title, desc = apply_upset_decoration("Congratulations!", "D", 0.30)
    assert title == "🚨 UPSET VICTORY — Congratulations!"
    assert "They won as ~2:1 underdogs!" in desc


def test_test_user_rating_floor_makes_bots_legendary_underdogs():
    # A full team pinned to the TEST_MODE floor must always compute as a
    # sub-legendary-threshold underdog against even brand-new real players,
    # so any bot win in TEST_MODE demonstrates the callout.
    from helpers.skill import TEST_USER_RATING_FLOOR

    p = team_win_probability([TEST_USER_RATING_FLOOR] * 3, [PRIOR] * 3)
    assert p < LEGENDARY_UPSET_THRESHOLD
    # ...and stays legendary even with one real (prior-rated) player carried
    # on the bot team, e.g. mixed test lobbies.
    p_mixed = team_win_probability([TEST_USER_RATING_FLOOR] * 2 + [PRIOR], [PRIOR] * 3)
    assert p_mixed < LEGENDARY_UPSET_THRESHOLD
