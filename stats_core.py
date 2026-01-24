"""
Core statistics utilities shared between player_stats and legacy_stats modules.

This module contains pure utility functions with no dependencies on other stats modules,
breaking the circular dependency between player_stats.py and legacy_stats.py.
"""
from datetime import datetime, timedelta


def get_timeframe_start_date(time_frame):
    """
    Get the start date for a given time frame.

    Args:
        time_frame: 'week', 'month', or None (lifetime)

    Returns:
        datetime object representing the start of the time frame
    """
    now = datetime.now()

    if time_frame == 'week':
        return now - timedelta(days=7)
    elif time_frame == 'month':
        return now - timedelta(days=30)
    else:  # Lifetime stats
        return datetime(2000, 1, 1)  # Far in the past


def calculate_win_percentage(wins, losses, draws=0):
    """
    Calculate win percentage from wins, losses, and draws.

    Args:
        wins: Number of wins
        losses: Number of losses
        draws: Number of draws (default 0)

    Returns:
        float: Win percentage (0-100), or 0 if no games played
    """
    total_games = wins + losses + draws
    if total_games == 0:
        return 0.0
    return (wins / total_games) * 100


def calculate_team_draft_win_percentage(wins, losses, tied=0):
    """
    Calculate team draft win percentage.

    Args:
        wins: Number of draft wins
        losses: Number of draft losses
        tied: Number of tied drafts (default 0)

    Returns:
        float: Win percentage (0-100), or 0 if no drafts played
    """
    total_drafts = wins + losses + tied
    if total_drafts == 0:
        return 0.0
    return (wins / total_drafts) * 100
