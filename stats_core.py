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

    A draw counts as half a win: 5-0-1 (91.7%) beats 5-1-0 (83.3%)
    instead of tying it, but never beats 6-0-0.

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
    return ((wins + 0.5 * draws) / total_games) * 100


def calculate_team_draft_win_percentage(wins, losses, tied=0):
    """
    Calculate team draft win percentage.

    A tied draft is the draft-level draw, so this is calculate_win_percentage
    under the name every caller reads in: won/lost/tied.

    Args:
        wins: Number of draft wins
        losses: Number of draft losses
        tied: Number of tied drafts (default 0)

    Returns:
        float: Win percentage (0-100), or 0 if no drafts played
    """
    return calculate_win_percentage(wins, losses, tied)
