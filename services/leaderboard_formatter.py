import discord
from datetime import datetime
from .leaderboard_service import get_leaderboard_data, get_minimum_requirements, STREAK_MINIMUMS, PERFECT_STREAK_MINIMUMS
from leaderboard_config import CATEGORY_CONFIGS as LEADERBOARD_CATEGORIES

# Default leaderboard timeframe display names
TIMEFRAME_DISPLAY = {
    "active": "Active Streaks",
    "7d": "Last 7 Days",
    "14d": "Last 14 Days",
    "30d": "Last 30 Days",
    "90d": "Last 90 Days",
    "lifetime": "Lifetime"
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
    if category == "longest_win_streak":
        # Use streak-specific minimums instead of match count minimums
        min_streak = STREAK_MINIMUMS.get(effective_timeframe, 10)
        description = category_config['description_template'].format(streak_min=min_streak)
    elif category == "perfect_streak":
        # Use perfect streak-specific minimums
        min_streak = PERFECT_STREAK_MINIMUMS.get(effective_timeframe, 8)
        description = category_config['description_template'].format(streak_min=min_streak)
    else:
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
        # Format leaderboard entries (limit to top 10 to avoid Discord field size limits)
        entries = []
        for i, player in enumerate(leaderboard_data[:10], 1):
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