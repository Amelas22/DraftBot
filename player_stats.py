import discord
from datetime import datetime, timedelta
from sqlalchemy import select, func, and_, or_, text
from session import AsyncSessionLocal, DraftSession, MatchResult, PlayerStats
from loguru import logger

async def get_player_statistics(user_id, time_frame=None, user_display_name=None):
    """Get player statistics for a specific user and time frame."""
    try:
        now = datetime.now()
        
        # Calculate the start date based on time frame
        if time_frame == 'week':
            start_date = now - timedelta(days=7)
        elif time_frame == 'month':
            start_date = now - timedelta(days=30)
        else:  # Lifetime stats
            start_date = datetime(2000, 1, 1)  # Far in the past
        
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Get basic player stats if they exist
                player_stats_query = select(PlayerStats).where(PlayerStats.player_id == user_id)
                player_stats_result = await session.execute(player_stats_query)
                player_stats = player_stats_result.scalar_one_or_none()
                
                # For SQLite, we need to use json_extract with LIKE to search for values in JSON
                # Use direct SQL for the complex JSON conditions
                drafts_query = text("""
                    SELECT COUNT(*) FROM draft_sessions 
                    WHERE (
                        json_extract(sign_ups, '$') LIKE :pattern
                        OR json_extract(team_a, '$') LIKE :pattern
                        OR json_extract(team_b, '$') LIKE :pattern
                    ) AND draft_start_time >= :start_date
                """)
                
                pattern = f'%"{user_id}"%'  # Pattern to match user_id in JSON string
                drafts_played_result = await session.execute(
                    drafts_query, 
                    {"pattern": pattern, "start_date": start_date}
                )
                drafts_played = drafts_played_result.scalar() or 0
                
                # Get matches won
                matches_won_query = select(func.count(MatchResult.id)).where(
                    and_(
                        MatchResult.winner_id == user_id,
                        # Join with DraftSession to filter by date
                        MatchResult.session_id.in_(
                            select(DraftSession.session_id).where(
                                DraftSession.draft_start_time >= start_date
                            )
                        )
                    )
                )
                matches_won_result = await session.execute(matches_won_query)
                matches_won = matches_won_result.scalar() or 0
                
                # Get trophies won by display name
                # First, we need to get the user's display name if not provided
                if not user_display_name:
                    # Try to get it from PlayerStats or sign_ups
                    if player_stats and player_stats.display_name:
                        user_display_name = player_stats.display_name
                    else:
                        # Try to find it in the sign_ups table
                        sign_ups_query = text("""
                            SELECT sign_ups FROM draft_sessions 
                            WHERE json_extract(sign_ups, '$') LIKE :pattern
                            LIMIT 1
                        """)
                        sign_ups_result = await session.execute(
                            sign_ups_query, 
                            {"pattern": pattern}
                        )
                        sign_ups_data = sign_ups_result.scalar()
                        if sign_ups_data and isinstance(sign_ups_data, dict) and user_id in sign_ups_data:
                            user_display_name = sign_ups_data[user_id]
                        else:
                            # Fallback to a generic name if we can't find the display name
                            user_display_name = "Unknown"
                
                # Now search for trophies by display name
                trophies_query = text("""
                    SELECT COUNT(*) FROM draft_sessions 
                    WHERE json_extract(trophy_drafters, '$') LIKE :name_pattern
                    AND draft_start_time >= :start_date
                """)
                
                name_pattern = f'%"{user_display_name}"%'  # Pattern to match display name in JSON string
                trophies_result = await session.execute(
                    trophies_query, 
                    {"name_pattern": name_pattern, "start_date": start_date}
                )
                trophies_won = trophies_result.scalar() or 0
                
                # Get total matches played
                matches_played_query = select(func.count(MatchResult.id)).where(
                    and_(
                        or_(
                            MatchResult.player1_id == user_id,
                            MatchResult.player2_id == user_id
                        ),
                        # Join with DraftSession to filter by date
                        MatchResult.session_id.in_(
                            select(DraftSession.session_id).where(
                                DraftSession.draft_start_time >= start_date
                            )
                        )
                    )
                )
                matches_played_result = await session.execute(matches_played_query)
                matches_played = matches_played_result.scalar() or 0
                
                # Calculate match win percentage
                match_win_percentage = (matches_won / matches_played * 100) if matches_played > 0 else 0
                
                # Get current ELO rating (or default)
                current_elo = player_stats.elo_rating if player_stats else 1200
                
                # Get stats by cube type
                # First, get all cubes the player has played
                cube_query = text("""
                    SELECT cube, COUNT(*) as draft_count 
                    FROM draft_sessions 
                    WHERE (
                        json_extract(sign_ups, '$') LIKE :pattern
                        OR json_extract(team_a, '$') LIKE :pattern
                        OR json_extract(team_b, '$') LIKE :pattern
                    ) 
                    AND draft_start_time >= :start_date
                    AND cube IS NOT NULL
                    GROUP BY LOWER(cube)
                    HAVING COUNT(*) >= 5
                """)
                
                cube_result = await session.execute(
                    cube_query, 
                    {"pattern": pattern, "start_date": start_date}
                )
                cube_data = cube_result.fetchall()
                
                # For each cube, get matches played and won
                cube_stats = {}
                for cube_name, draft_count in cube_data:
                    if not cube_name:  # Skip if cube name is None
                        continue
                        
                    # Normalize cube name
                    normalized_cube_name = cube_name.lower()
                    
                    # Get matches played for this cube
                    cube_matches_query = text("""
                        SELECT COUNT(*) 
                        FROM match_results 
                        WHERE (player1_id = :user_id OR player2_id = :user_id)
                        AND session_id IN (
                            SELECT session_id FROM draft_sessions 
                            WHERE LOWER(cube) = :cube_name
                            AND draft_start_time >= :start_date
                        )
                    """)
                    
                    cube_matches_result = await session.execute(
                        cube_matches_query, 
                        {"user_id": user_id, "cube_name": normalized_cube_name, "start_date": start_date}
                    )
                    cube_matches_played = cube_matches_result.scalar() or 0
                    
                    # Get matches won for this cube
                    cube_wins_query = text("""
                        SELECT COUNT(*) 
                        FROM match_results 
                        WHERE winner_id = :user_id
                        AND session_id IN (
                            SELECT session_id FROM draft_sessions 
                            WHERE LOWER(cube) = :cube_name
                            AND draft_start_time >= :start_date
                        )
                    """)
                    
                    cube_wins_result = await session.execute(
                        cube_wins_query, 
                        {"user_id": user_id, "cube_name": normalized_cube_name, "start_date": start_date}
                    )
                    cube_matches_won = cube_wins_result.scalar() or 0
                    
                    # Calculate win percentage
                    cube_win_percentage = (cube_matches_won / cube_matches_played * 100) if cube_matches_played > 0 else 0
                    
                    # Store stats with the original cube name (not normalized)
                    cube_stats[cube_name] = {
                        "drafts_played": draft_count,
                        "matches_played": cube_matches_played,
                        "matches_won": cube_matches_won,
                        "win_percentage": cube_win_percentage
                    }
                
                return {
                    "drafts_played": drafts_played,
                    "matches_played": matches_played,
                    "matches_won": matches_won,
                    "trophies_won": trophies_won,
                    "match_win_percentage": match_win_percentage,
                    "current_elo": current_elo,
                    "display_name": user_display_name,
                    "cube_stats": cube_stats
                }
    except Exception as e:
        logger.error(f"Error getting stats for user {user_id}: {e}")
        # Return default values
        return {
            "drafts_played": 0,
            "matches_played": 0,
            "matches_won": 0,
            "trophies_won": 0,
            "match_win_percentage": 0,
            "current_elo": 1200,
            "display_name": "Unknown",
            "cube_stats": {}
        }

async def create_stats_embed(user, stats_weekly, stats_monthly, stats_lifetime):
    """Create a Discord embed with player statistics."""
    embed = discord.Embed(
        title=f"Stats for {user.name if hasattr(user, 'name') else stats_lifetime['display_name']}",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    
    # Set thumbnail if user object is available
    if hasattr(user, 'display_avatar'):
        embed.set_thumbnail(url=user.display_avatar.url)
    
    # Weekly stats
    embed.add_field(
        name="Weekly Stats (Last 7 Days)",
        value=(
            f"Drafts Played: {stats_weekly['drafts_played']}\n"
            f"Matches Won: {stats_weekly['matches_won']}/{stats_weekly['matches_played']}\n"
            f"Win %: {stats_weekly['match_win_percentage']:.1f}%\n"
            f"Trophies: {stats_weekly['trophies_won']}"
        ),
        inline=True
    )
    
    # Monthly stats
    embed.add_field(
        name="Monthly Stats (Last 30 Days)",
        value=(
            f"Drafts Played: {stats_monthly['drafts_played']}\n"
            f"Matches Won: {stats_monthly['matches_won']}/{stats_monthly['matches_played']}\n"
            f"Win %: {stats_monthly['match_win_percentage']:.1f}%\n"
            f"Trophies: {stats_monthly['trophies_won']}"
        ),
        inline=True
    )
    
    # Lifetime stats
    embed.add_field(
        name="Lifetime Stats",
        value=(
            f"Drafts Played: {stats_lifetime['drafts_played']}\n"
            f"Matches Won: {stats_lifetime['matches_won']}/{stats_lifetime['matches_played']}\n"
            f"Win %: {stats_lifetime['match_win_percentage']:.1f}%\n"
            f"Trophies: {stats_lifetime['trophies_won']}\n"
            f"Current ELO: {stats_lifetime['current_elo']:.0f}"
        ),
        inline=False
    )
    
    # Add cube-specific stats if any are available
    if stats_lifetime['cube_stats']:
        # Convert to list and sort by drafts_played in descending order
        sorted_cube_stats = sorted(
            stats_lifetime['cube_stats'].items(),
            key=lambda x: x[1]['drafts_played'],
            reverse=True
        )
        
        cube_stats_text = ""
        for cube_name, stats in sorted_cube_stats:
            cube_stats_text += f"**{cube_name}**: {stats['win_percentage']:.1f}% ({stats['drafts_played']} Drafts)\n"
        
        embed.add_field(
            name="Cube Win Percentage",
            value=cube_stats_text,
            inline=False
        )
    
    embed.set_footer(text="Stats are updated after each draft")
    
    return embed