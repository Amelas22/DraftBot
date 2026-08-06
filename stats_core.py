"""
Pure statistics utilities used by player_stats (no dependencies on other
stats modules).
"""
from datetime import datetime, timedelta


def get_timeframe_start_date(time_frame):
    """
    Get the start date for a given time frame.

    Args:
        time_frame: 'week', 'month', or None (lifetime)

    Returns:
        datetime object representing the start of the time frame, or None
        for lifetime. None (not a far-past sentinel like datetime(2000,1,1))
        so the fold's `since` behaves identically for every lifetime caller:
        /stats, /record head-to-head, and the leaderboards all pass None for
        "lifetime" and fetch_session_records treats that uniformly -- a
        sentinel date used to diverge here (NULL-event-time rows dropped
        for /stats but kept for h2h/leaderboards, which passed None already).
    """
    now = datetime.now()

    if time_frame == 'week':
        return now - timedelta(days=7)
    elif time_frame == 'month':
        return now - timedelta(days=30)
    else:  # Lifetime stats
        return None


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
