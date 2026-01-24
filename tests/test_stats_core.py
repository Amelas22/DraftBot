"""
Unit tests for stats_core module - shared statistics utility functions.
"""
import pytest
from datetime import datetime, timedelta
from stats_core import (
    get_timeframe_start_date,
    calculate_win_percentage,
    calculate_team_draft_win_percentage
)


class TestGetTimeframeStartDate:
    """Tests for get_timeframe_start_date function"""

    def test_week_timeframe(self):
        """Test that 'week' returns date 7 days ago"""
        result = get_timeframe_start_date('week')
        expected = datetime.now() - timedelta(days=7)

        # Allow 1 second tolerance for test execution time
        assert abs((result - expected).total_seconds()) < 1

    def test_month_timeframe(self):
        """Test that 'month' returns date 30 days ago"""
        result = get_timeframe_start_date('month')
        expected = datetime.now() - timedelta(days=30)

        # Allow 1 second tolerance for test execution time
        assert abs((result - expected).total_seconds()) < 1

    def test_lifetime_timeframe(self):
        """Test that None/lifetime returns date far in past"""
        result = get_timeframe_start_date(None)
        expected = datetime(2000, 1, 1)

        assert result == expected

    def test_invalid_timeframe_defaults_to_lifetime(self):
        """Test that invalid timeframe string defaults to lifetime"""
        result = get_timeframe_start_date('invalid')
        expected = datetime(2000, 1, 1)

        assert result == expected


class TestCalculateWinPercentage:
    """Tests for calculate_win_percentage function"""

    def test_perfect_record(self):
        """Test 100% win rate"""
        result = calculate_win_percentage(wins=10, losses=0)
        assert result == 100.0

    def test_zero_wins(self):
        """Test 0% win rate"""
        result = calculate_win_percentage(wins=0, losses=10)
        assert result == 0.0

    def test_fifty_percent(self):
        """Test 50% win rate"""
        result = calculate_win_percentage(wins=5, losses=5)
        assert result == 50.0

    def test_with_draws(self):
        """Test win percentage with draws included"""
        # 5 wins, 3 losses, 2 draws = 5/10 = 50%
        result = calculate_win_percentage(wins=5, losses=3, draws=2)
        assert result == 50.0

    def test_no_games_played(self):
        """Test that 0 games returns 0%"""
        result = calculate_win_percentage(wins=0, losses=0)
        assert result == 0.0

    def test_fractional_percentage(self):
        """Test fractional win percentage"""
        # 7 wins out of 32 total = 21.875%
        result = calculate_win_percentage(wins=7, losses=25)
        assert abs(result - 21.875) < 0.001

    def test_only_draws(self):
        """Test that only draws returns 0%"""
        result = calculate_win_percentage(wins=0, losses=0, draws=5)
        assert result == 0.0

    def test_one_win_many_losses(self):
        """Test asymmetric record"""
        # 1 win, 99 losses = 1%
        result = calculate_win_percentage(wins=1, losses=99)
        assert result == 1.0


class TestCalculateTeamDraftWinPercentage:
    """Tests for calculate_team_draft_win_percentage function"""

    def test_all_wins(self):
        """Test 100% draft win rate"""
        result = calculate_team_draft_win_percentage(wins=10, losses=0)
        assert result == 100.0

    def test_all_losses(self):
        """Test 0% draft win rate"""
        result = calculate_team_draft_win_percentage(wins=0, losses=10)
        assert result == 0.0

    def test_fifty_percent_drafts(self):
        """Test 50% draft win rate"""
        result = calculate_team_draft_win_percentage(wins=5, losses=5)
        assert result == 50.0

    def test_with_ties(self):
        """Test win percentage with tied drafts"""
        # 3 wins, 2 losses, 1 tie = 3/6 = 50%
        result = calculate_team_draft_win_percentage(wins=3, losses=2, tied=1)
        assert result == 50.0

    def test_no_drafts_played(self):
        """Test that 0 drafts returns 0%"""
        result = calculate_team_draft_win_percentage(wins=0, losses=0)
        assert result == 0.0

    def test_only_ties(self):
        """Test that only ties returns 0%"""
        result = calculate_team_draft_win_percentage(wins=0, losses=0, tied=5)
        assert result == 0.0

    def test_tinylegs_actual_stats(self):
        """Test with TinyLegs' actual stats: 3 wins, 11 losses, 0 ties"""
        # 3 wins out of 14 total = 21.428...%
        result = calculate_team_draft_win_percentage(wins=3, losses=11, tied=0)
        expected = (3 / 14) * 100
        assert abs(result - expected) < 0.001

    def test_fractional_percentage_with_ties(self):
        """Test fractional percentage with ties"""
        # 7 wins, 8 losses, 2 ties = 7/17 = 41.176...%
        result = calculate_team_draft_win_percentage(wins=7, losses=8, tied=2)
        expected = (7 / 17) * 100
        assert abs(result - expected) < 0.001


class TestEdgeCases:
    """Edge case tests for all functions"""

    def test_negative_values_not_validated(self):
        """Note: Functions don't validate negative inputs (trusted internal use)"""
        # These would produce nonsensical results but won't crash
        result = calculate_win_percentage(wins=-1, losses=10)
        # Just verify it returns a number
        assert isinstance(result, float)

    def test_very_large_numbers(self):
        """Test with very large game counts"""
        result = calculate_win_percentage(wins=1000000, losses=1000000)
        assert result == 50.0

    def test_timeframe_case_sensitivity(self):
        """Test that timeframe matching is case-sensitive"""
        # 'Week' (capital) should default to lifetime, not week
        result = get_timeframe_start_date('Week')
        expected = datetime(2000, 1, 1)
        assert result == expected
