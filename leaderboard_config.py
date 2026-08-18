"""
Central registry for all leaderboard categories.
This is the SINGLE SOURCE OF TRUTH for category definitions.

When adding a new leaderboard category:
1. Add to CATEGORY_CONFIGS dict below
2. Add it to a group in LEADERBOARD_GROUPS (this sets where it appears)
3. Add query logic to services/leaderboard_service.py
4. Add database columns to models/leaderboard_message.py (2 per category)
5. Run migration

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
    "trophy_quiz_points": {
        "title": "Trophy Quiz Leaderboard",
        "description_template": "Players with the most trophy-quiz points",
        "color": discord.Color.from_rgb(212, 175, 55),  # Trophy gold
        "formatter": lambda p, rank: f"{get_medal(rank)}{p['display_name']}: {p['total_points']} points ({p['total_quizzes']} quizzes)"
    },
    "top_elo": {
        "title": "Top Elo Leaderboard",
        # The qualifying rating floor is deliberately unstated: naming it would
        # turn the board into a number to defend rather than a board to reach.
        "description_template": "Highest rated players who have drafted in the last 2 weeks",
        "color": discord.Color.teal(),
        "formatter": lambda p, rank: f"{get_medal(rank)}**{p['display_name']}**: {p['rating']}"
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

# How the boards cluster in the channel. Each group gets a header message and
# its boards are posted beneath it, in this order — so this list, not the
# CATEGORY_CONFIGS dict, decides what the channel looks like.
#
# A new category must be added to a group: ALL_CATEGORIES is derived from here,
# and test_leaderboard_groups asserts the two stay in sync, so an ungrouped
# category fails the suite rather than silently vanishing from the channel.
LEADERBOARD_GROUPS = [
    {
        "key": "performance",
        "title": "🏆 Performance",
        "blurb": "Who's winning — win rates, partnerships, and volume.",
        "categories": ["draft_record", "match_win", "top_elo", "time_vault_and_key", "drafts_played"],
    },
    {
        "key": "streaks",
        "title": "🔥 Streaks",
        "blurb": "Runs in progress and the best ever set.",
        "categories": ["hot_streak", "draft_win_streak", "longest_win_streak", "perfect_streak"],
    },
    {
        "key": "quizzes",
        "title": "🧠 Quizzes",
        "blurb": "Reading the table without playing it.",
        "categories": ["quiz_points", "trophy_quiz_points"],
    },
]

# Derived list for iteration, in the order the groups declare (guaranteed by
# test to cover exactly the CATEGORY_CONFIGS keys).
ALL_CATEGORIES = [c for group in LEADERBOARD_GROUPS for c in group["categories"]]

# Categories that should auto-update after draft completion
AUTO_UPDATE_CATEGORIES = ALL_CATEGORIES  # All categories by default

# Categories that award crowns when player is #1
# Excludes: hot_streak (too volatile), streak leaderboards (temporary achievements)
CROWN_ELIGIBLE_CATEGORIES = [
    "draft_record",
    "match_win",
    "drafts_played",
    "time_vault_and_key",
    "quiz_points"
]

# Timeframe options for leaderboards
# Format: (value, display_label)
STANDARD_TIMEFRAMES = [
    ("14d", "14 Days"),
    ("30d", "30 Days"),
    ("90d", "90 Days"),
    ("lifetime", "Lifetime")
]

# Streak categories have an "Active" option instead of 14d
STREAK_TIMEFRAMES = [
    ("active", "Active"),
    ("30d", "30 Days"),
    ("90d", "90 Days"),
    ("lifetime", "Lifetime")
]

# Categories that use streak timeframes
STREAK_CATEGORIES = ["longest_win_streak", "perfect_streak", "draft_win_streak"]

# Boards that define their own window, so the timeframe selector does not apply.
# The value here is what the query and the rendered title both use, whatever the
# caller passed -- otherwise the title would advertise a window the board never
# applied. These boards also drop the "choose a filter" footer, since filtering
# them changes nothing.
PINNED_TIMEFRAMES = {
    "hot_streak": "7d",
    "top_elo": "14d",
}

# Valid timeframe values (for validation)
VALID_TIMEFRAMES = ["14d", "30d", "90d", "lifetime", "active"]

# Crown role icons - used for both Discord roles and display name prefixes
# For custom emojis: Use format "<:emoji_name:emoji_id>" from guild 1355718878298116096
CROWN_ICONS = {
    1: "👑",   # Crown
    2: "<:doublecrown:1460684949505048871>",   # Double Crown (custom emoji)
    3: "<:triplecrown:1460715033712525464>",   # Triple Crown (custom emoji)
    4: "🌟",   # Grand Champion
    5: "⚜️",   # Ultimate Champion
}

# Default crown role names
DEFAULT_CROWN_ROLE_NAMES = {
    "1": "Crown",
    "2": "Double Crown",
    "3": "Triple Crown",
    "4": "Grand Champion",
    "5": "Ultimate Champion"
}
