"""
Central registry for all leaderboard categories.
This is the SINGLE SOURCE OF TRUTH for category definitions.

When adding a new leaderboard category:
1. Add to CATEGORY_CONFIGS dict below
2. Add query logic to services/leaderboard_service.py
3. Add database columns to models/leaderboard_message.py (2 per category)
4. Run migration

The category will automatically propagate to:
- /leaderboard command (cogs/leaderboard.py)
- Auto-updates after drafts (utils.py)
- Display formatting (services/leaderboard_formatter.py)
"""

import discord

# Helper function for formatting (used by all categories)
def get_medal(rank):
    """Return rank with medal emoji for top 3 positions"""
    if rank == 1:
        return "1. 🥇 "
    elif rank == 2:
        return "2. 🥈 "
    elif rank == 3:
        return "3. 🥉 "
    else:
        return f"{rank}. "


def _format_ended_streak(p):
    """Format the 'ended by' text for completed streaks."""
    ended_time = f"<t:{int(p['ended_at'].timestamp())}:R>"

    if p.get('ended_by_name'):
        return f"(ended {ended_time} by {p['ended_by_name']})"
    else:
        # Fallback for old records or deleted players
        return f"(ended {ended_time})"


# Category configuration - SINGLE SOURCE OF TRUTH
CATEGORY_CONFIGS = {
    "draft_record": {
        "title": "Draft Record Leaderboard",
        "description_template": "Players with the highest team draft win percentage (min {drafts} drafts, 50%+ win rate)",
        "color": discord.Color.blue(),
        "formatter": lambda p, rank: f"{get_medal(rank)}**{p['display_name']}**: {p['team_drafts_won']}-{p['team_drafts_lost']}-{p['team_drafts_tied']} ({p['team_draft_win_percentage']:.1f}%)"
    },
    "match_win": {
        "title": "Match Win Leaderboard",
        "description_template": "Players with the highest individual match win percentage (min {matches} matches, 50%+ win rate)",
        "color": discord.Color.green(),
        "formatter": lambda p, rank: f"{get_medal(rank)}{p['display_name']}: {p['matches_won']}/{p['completed_matches']} ({p['match_win_percentage']:.1f}%)"
    },
    "drafts_played": {
        "title": "Drafts Played Leaderboard",
        "description_template": "Players who have participated in the most drafts",
        "color": discord.Color.purple(),
        "formatter": lambda p, rank: f"{get_medal(rank)}{p['display_name']}: {p['drafts_played']} drafts"
    },
    "time_vault_and_key": {
        "title": "Vault / Key Leaderboard",
        "description_template": "Highest Draft Win Rate when paired as teammates (min {partnership_drafts} drafts together, 50%+ win rate)",
        "color": discord.Color.gold(),
        "formatter": lambda p, rank: f"{get_medal(rank)}{p['player_name']} & {p['teammate_name']}: {p['drafts_won']}-{p['drafts_lost']}-{p['drafts_tied']} ({p['win_percentage']:.1f}%)"
    },
    "hot_streak": {
        "title": "Hot Streak Leaderboard",
        "description_template": "Players with the best match win % in the last 7 days (min 9 matches, 50%+ win rate)",
        "color": discord.Color.red(),
        "formatter": lambda p, rank: f"{get_medal(rank)}{p['display_name']}: {p['matches_won']}/{p['completed_matches']} ({p['match_win_percentage']:.1f}%)"
    },
    "longest_win_streak": {
        "title": "Win Streak Leaderboard",
        "description_template": "Longest consecutive match win streaks (min {streak_min}-win streak)",
        "color": discord.Color.orange(),
        "formatter": lambda p, rank: (
            f"{get_medal(rank)}{p['display_name']}: {p['longest_win_streak']}-win streak " +
            ("🔥 (ACTIVE)" if p.get('is_active') else
             (_format_ended_streak(p) if p.get('ended_at') else ""))
        )
    },
    "perfect_streak": {
        "title": "Perfect Streak Leaderboard",
        "description_template": "Longest consecutive 2-0 match win streaks (min {streak_min} 2-0 wins)",
        "color": discord.Color.from_rgb(255, 215, 0),
        "formatter": lambda p, rank: (
            f"{get_medal(rank)}{p['display_name']}: {p['perfect_streak']}-win perfect streak " +
            ("🔥🔥 (ACTIVE)" if p.get('is_active') else
             (_format_ended_streak(p) if p.get('ended_at') else ""))
        )
    },
    "quiz_points": {
        "title": "Quiz Points Leaderboard",
        "description_template": "Players with the most quiz points (min {quizzes} quizzes completed)",
        "color": discord.Color.from_rgb(138, 43, 226),  # Blue-violet
        "formatter": lambda p, rank: f"{get_medal(rank)}{p['display_name']}: {p['total_points']} points ({p['total_quizzes']} quizzes, {p['accuracy_percentage']:.1f}% accuracy)"
    },
    "draft_win_streak": {
        "title": "Order of the White Lotus",
        "description_template": "Longest consecutive draft win streaks (min {streak_min} draft wins)",
        "color": discord.Color.from_rgb(255, 253, 208),  # Pale yellow (lotus-like)
        "formatter": lambda p, rank: (
            f"{get_medal(rank)}{p['display_name']}: {p['draft_win_streak']}-draft streak " +
            ("᪥ (ACTIVE)" if p.get('is_active') else
             (f"(ended <t:{int(p['ended_at'].timestamp())}:R>)" if p.get('ended_at') else ""))
        )
    }
}

# Derived list for iteration (guaranteed to match dict keys)
ALL_CATEGORIES = list(CATEGORY_CONFIGS.keys())

# Categories that should auto-update after draft completion
AUTO_UPDATE_CATEGORIES = ALL_CATEGORIES  # All categories by default
