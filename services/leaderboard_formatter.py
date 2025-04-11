import discord
from datetime import datetime
from .leaderboard_service import get_leaderboard_data, get_minimum_requirements

# Default leaderboard timeframe display names
TIMEFRAME_DISPLAY = {
    "7d": "Last 7 Days",
    "14d": "Last 14 Days",
    "30d": "Last 30 Days",
    "90d": "Last 90 Days",
    "lifetime": "Lifetime"
}

# Helper function to add medals
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

# Define category configurations
LEADERBOARD_CATEGORIES = {
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
        # Hot streak always uses 7 days, so we hardcode that in the description
        "description_template": "Players with the best match win % in the last 7 days (min 9 matches, 50%+ win rate)",
        "color": discord.Color.red(),
        "formatter": lambda p, rank: f"{get_medal(rank)}{p['display_name']}: {p['matches_won']}/{p['completed_matches']} ({p['match_win_percentage']:.1f}%)"
    }
}

async def create_leaderboard_embed(guild_id, category="draft_record", limit=20, timeframe="lifetime"):
    """Create an embed with leaderboard data"""
    # Ensure category is valid
    if category not in LEADERBOARD_CATEGORIES:
        category = "draft_record"  # Default to draft_record if invalid category
    
    # Use 7d timeframe for hot_streak regardless of what was passed
    if category == "hot_streak":
        effective_timeframe = "7d"
    else:
        effective_timeframe = timeframe
    
    # Get the leaderboard data
    leaderboard_data = await get_leaderboard_data(guild_id, category, limit, effective_timeframe)
    
    # Get minimum requirements for description
    min_requirements = get_minimum_requirements(effective_timeframe)
    
    # Get category configuration
    category_config = LEADERBOARD_CATEGORIES[category]
    
    # Format timeframe for display
    timeframe_display = TIMEFRAME_DISPLAY.get(effective_timeframe, "Lifetime")
    
    # Format title and description with timeframe
    title = f"{category_config['title']} ({timeframe_display})"
    
    # Format description with minimum requirements where needed
    description = category_config['description_template'].format(**min_requirements)
    
    # Create the embed
    embed = discord.Embed(
        title=title,
        description=description,
        color=category_config['color'],
        timestamp=datetime.now()
    )
    
    # Add leaderboard data
    if not leaderboard_data:
        embed.add_field(name="No Data", value="No players found matching the criteria")
    else:
        # Format leaderboard entries
        entries = []
        for i, player in enumerate(leaderboard_data, 1):
            entry = category_config['formatter'](player, i)
            entries.append(entry)
        
        # Add all entries in a single field
        embed.add_field(name="Rankings", value="\n".join(entries), inline=False)
    
    # Only add this footer text for categories with timeframe buttons
    if category != "hot_streak":
        embed.set_footer(text="Choose a filter to refresh stats")
    else:
        embed.set_footer(text="Updated regularly")
    
    return embed