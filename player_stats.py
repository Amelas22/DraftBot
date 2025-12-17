import discord
import json
from datetime import datetime, timedelta
from sqlalchemy import select, func, and_, or_, text
from session import AsyncSessionLocal, DraftSession, MatchResult, PlayerStats
from loguru import logger

async def get_player_statistics(user_id, time_frame=None, user_display_name=None, guild_id=None):
    """Get player statistics for a specific user and time frame, filtered by guild_id if provided."""
    try:
        now = datetime.now()
        
        # Calculate the start date based on time frame
        if time_frame == 'week':
            start_date = now - timedelta(days=7)
        elif time_frame == 'month':
            start_date = now - timedelta(days=30)
        else:  # Lifetime stats
            start_date = datetime(2000, 1, 1)  # Far in the past
        
        # Default values for stats
        drafts_played = 0
        matches_played = 0
        matches_won = 0
        trophies_won = 0
        match_win_percentage = 0
        current_elo = 1200
        display_name = user_display_name or "Unknown"
        cube_stats = {}
        
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Get basic player stats if they exist 
                player_stats_query = select(PlayerStats).where(
                    PlayerStats.player_id == user_id, 
                    PlayerStats.guild_id == guild_id
                )                
                player_stats_result = await session.execute(player_stats_query)
                player_stats = player_stats_result.scalar_one_or_none()
                
                if player_stats:
                    if player_stats.display_name:
                        display_name = player_stats.display_name

                # Extract streak data from PlayerStats
                current_win_streak = 0
                longest_win_streak = 0
                current_perfect_streak = 0
                longest_perfect_streak = 0

                if player_stats:
                    current_win_streak = player_stats.current_win_streak or 0
                    longest_win_streak = player_stats.longest_win_streak or 0
                    current_perfect_streak = player_stats.current_perfect_streak or 0
                    longest_perfect_streak = player_stats.longest_perfect_streak or 0

                # Define the pattern for JSON searches
                pattern = f'%"{user_id}"%'  # Pattern to match user_id in JSON string
                
                base_query = """
                    FROM draft_sessions 
                    WHERE (
                        json_extract(sign_ups, '$') LIKE :pattern
                        OR json_extract(team_a, '$') LIKE :pattern
                        OR json_extract(team_b, '$') LIKE :pattern
                    ) 
                    AND teams_start_time >= :start_date
                    AND session_type IN ('random', 'staked')
                    AND victory_message_id_results_channel IS NOT NULL
                    AND guild_id = :guild_id
                """
                
                # Count completed drafts
                drafts_query = text(f"SELECT COUNT(*) {base_query}")
                
                # Prepare query parameters
                query_params = {"pattern": pattern, "start_date": start_date, "guild_id": guild_id}               
                drafts_played_result = await session.execute(drafts_query, query_params)
                drafts_played = drafts_played_result.scalar() or 0
                
                # Get matches won - update to include guild_id filter
                matches_won_query_text = """
                    SELECT COUNT(*) 
                    FROM match_results 
                    WHERE winner_id = :user_id
                    AND session_id IN (
                        SELECT session_id FROM draft_sessions 
                        WHERE teams_start_time >= :start_date
                        AND session_type IN ('random', 'staked')
                        AND victory_message_id_results_channel IS NOT NULL
                        AND guild_id = :guild_id
                """
                
                matches_won_query_text += ")"
                matches_won_query = text(matches_won_query_text)
                
                # Update parameters for matches won query
                matches_won_params = {"user_id": user_id, "start_date": start_date, "guild_id": guild_id}               
                matches_won_result = await session.execute(matches_won_query, matches_won_params)
                matches_won = matches_won_result.scalar() or 0
                
                # First, try to get the display name from any sign_ups entries, with guild_id filter
                name_query_text = """
                    SELECT sign_ups FROM draft_sessions 
                    WHERE json_extract(sign_ups, '$') LIKE :pattern
                    AND guild_id = :guild_id
                """                
                name_query_text += " ORDER BY teams_start_time DESC LIMIT 1"
                name_query = text(name_query_text)
                
                name_params = {"pattern": pattern, "guild_id": guild_id}              
                name_result = await session.execute(name_query, name_params)
                sign_ups_json = name_result.scalar()
                
                # Extract display name from sign_ups JSON
                if sign_ups_json:
                    try:
                        # Handle string or dict format
                        if isinstance(sign_ups_json, str):
                            sign_ups = json.loads(sign_ups_json)
                        else:
                            sign_ups = sign_ups_json
                            
                        # Find the user's display name
                        if user_id in sign_ups:
                            display_name = sign_ups[user_id]
                            logger.info(f"Found display name '{display_name}' for user {user_id}")
                    except Exception as e:
                        logger.error(f"Error parsing sign_ups JSON: {e}")
                
                # Get trophies with guild_id filter
                trophies_query_text = """
                    SELECT trophy_drafters FROM draft_sessions 
                    WHERE trophy_drafters IS NOT NULL
                    AND teams_start_time >= :start_date
                    AND session_type IN ('random', 'staked')
                    AND victory_message_id_results_channel IS NOT NULL
                    AND guild_id = :guild_id
                """
                
                trophies_query = text(trophies_query_text)
                
                trophies_params = {"start_date": start_date, "guild_id": guild_id}               
                trophies_result = await session.execute(trophies_query, trophies_params)
                trophies_entries = trophies_result.fetchall()
                
                trophies_won = 0
                for (trophy_drafters_json,) in trophies_entries:
                    if not trophy_drafters_json:
                        continue
                        
                    try:
                        # Parse trophy_drafters - could be a string or already JSON
                        if isinstance(trophy_drafters_json, str):
                            trophy_drafters = json.loads(trophy_drafters_json)
                        elif isinstance(trophy_drafters_json, list):
                            trophy_drafters = trophy_drafters_json
                        else:
                            logger.warning(f"Unexpected trophy_drafters format: {type(trophy_drafters_json)}")
                            continue
                            
                        # Check if display name is in the list
                        if display_name in trophy_drafters:
                            trophies_won += 1
                            logger.info(f"Found trophy for '{display_name}' in {trophy_drafters}")
                    except Exception as e:
                        logger.error(f"Error processing trophy_drafters: {e}")
                
                # Approach 2: Cross-reference with sign_ups to find all display names
                if trophies_won == 0:
                    # Get all display names this user has used
                    names_query = text("""
                        SELECT sign_ups FROM draft_sessions 
                        WHERE json_extract(sign_ups, '$') LIKE :pattern
                    """)
                    
                    names_result = await session.execute(names_query, {"pattern": pattern})
                    all_sign_ups = names_result.fetchall()
                    
                    user_names = set()
                    for (signup_json,) in all_sign_ups:
                        if not signup_json:
                            continue
                            
                        try:
                            # Parse sign_ups
                            if isinstance(signup_json, str):
                                sign_ups = json.loads(signup_json)
                            else:
                                sign_ups = signup_json
                                
                            # Add this display name
                            if user_id in sign_ups:
                                user_names.add(sign_ups[user_id])
                        except Exception as e:
                            logger.error(f"Error processing sign_ups for names: {e}")
                    
                    # Now count trophies again with all user names
                    for (trophy_drafters_json,) in trophies_entries:
                        if not trophy_drafters_json:
                            continue
                            
                        try:
                            # Parse trophy_drafters
                            if isinstance(trophy_drafters_json, str):
                                trophy_drafters = json.loads(trophy_drafters_json)
                            elif isinstance(trophy_drafters_json, list):
                                trophy_drafters = trophy_drafters_json
                            else:
                                continue
                                
                            # Check if any of the user's names are in trophy_drafters
                            for name in user_names:
                                if name in trophy_drafters:
                                    trophies_won += 1
                                    logger.info(f"Found trophy for alternate name '{name}' in {trophy_drafters}")
                                    break  # Count each trophy only once
                        except Exception as e:
                            logger.error(f"Error processing trophy_drafters for alternates: {e}")
                
                # Get total matches played - Only count matches that have a winner determined.
                matches_played_query_text = """
                    SELECT COUNT(*) 
                    FROM match_results 
                    WHERE (player1_id = :user_id OR player2_id = :user_id)
                    AND winner_id IS NOT NULL
                    AND session_id IN (
                        SELECT session_id FROM draft_sessions 
                        WHERE teams_start_time >= :start_date
                        AND session_type IN ('random', 'staked')
                        AND victory_message_id_results_channel IS NOT NULL
                        AND guild_id = :guild_id
                """
                matches_played_query_text += ")"
                matches_played_query = text(matches_played_query_text)
                
                matches_played_params = {"user_id": user_id, "start_date": start_date, "guild_id": guild_id}                
                matches_played_result = await session.execute(matches_played_query, matches_played_params)
                matches_played = matches_played_result.scalar() or 0
                
                # Calculate match win percentage
                match_win_percentage = (matches_won / matches_played * 100) if matches_played > 0 else 0
                
                # Get all draft sessions with guild_id filter
                drafts_query_text = """
                    SELECT id, session_id, team_a, team_b
                    FROM draft_sessions 
                    WHERE (
                        json_extract(sign_ups, '$') LIKE :pattern
                        OR json_extract(team_a, '$') LIKE :pattern
                        OR json_extract(team_b, '$') LIKE :pattern
                    ) 
                    AND teams_start_time >= :start_date
                    AND session_type IN ('random', 'staked')
                    AND victory_message_id_results_channel IS NOT NULL
                    AND guild_id = :guild_id
                """

                drafts_query = text(drafts_query_text)
                
                drafts_params = {"pattern": pattern, "start_date": start_date, "guild_id": guild_id}                
                drafts_result = await session.execute(drafts_query, drafts_params)
                draft_sessions = drafts_result.fetchall()
                
                # Initialize counters
                team_drafts_played = 0
                team_drafts_won = 0
                team_drafts_tied = 0
                
                # Process each draft
                for draft_id, session_id, team_a_json, team_b_json in draft_sessions:
                    # Skip if missing team data
                    if not team_a_json or not team_b_json:
                        continue
                    
                    # Determine which team the user was on
                    try:
                        team_a = team_a_json if isinstance(team_a_json, list) else json.loads(team_a_json) if isinstance(team_a_json, str) else []
                        team_b = team_b_json if isinstance(team_b_json, list) else json.loads(team_b_json) if isinstance(team_b_json, str) else []
                        
                        user_team = None
                        if user_id in team_a:
                            user_team = 'A'
                        elif user_id in team_b:
                            user_team = 'B'
                        else:
                            # User not on either team (shouldn't happen)
                            continue
                            
                        # Pull all match results for this draft
                        match_results_query = text("""
                            SELECT 
                                player1_id, player2_id, 
                                player1_wins, player2_wins, 
                                winner_id
                            FROM match_results 
                            WHERE session_id = :session_id
                        """)
                        
                        match_results = await session.execute(
                            match_results_query, 
                            {"session_id": session_id}
                        )
                        
                        team_a_wins = 0
                        team_b_wins = 0
                        
                        for p1_id, p2_id, p1_wins, p2_wins, winner_id in match_results.fetchall():
                            # Determine which team won this match
                            if winner_id:
                                if winner_id in team_a:
                                    team_a_wins += 1
                                elif winner_id in team_b:
                                    team_b_wins += 1
                        
                        # Count this as a played draft
                        team_drafts_played += 1
                        
                        # Determine the draft winner
                        if team_a_wins > team_b_wins:
                            # Team A won
                            if user_team == 'A':
                                team_drafts_won += 1
                        elif team_b_wins > team_a_wins:
                            # Team B won
                            if user_team == 'B':
                                team_drafts_won += 1
                        else:
                            # It's a tie
                            team_drafts_tied += 1
                            
                    except Exception as e:
                        logger.error(f"Error processing draft {draft_id}: {e}")
                
                # Calculate team draft win percentage
                team_drafts_lost = team_drafts_played - team_drafts_won - team_drafts_tied
                team_draft_counted_drafts = team_drafts_won + team_drafts_lost
                team_draft_win_percentage = (team_drafts_won / team_draft_counted_drafts * 100) if team_draft_counted_drafts > 0 else 0
                
                # Get stats by cube type with guild_id filter
                cube_query_text = """
                    SELECT cube, COUNT(*) as draft_count 
                    FROM draft_sessions 
                    WHERE (
                        json_extract(sign_ups, '$') LIKE :pattern
                        OR json_extract(team_a, '$') LIKE :pattern
                        OR json_extract(team_b, '$') LIKE :pattern
                    ) 
                    AND teams_start_time >= :start_date
                    AND cube IS NOT NULL
                    AND session_type IN ('random', 'staked')
                    AND victory_message_id_results_channel IS NOT NULL
                    AND guild_id = :guild_id
                """
                
                cube_query_text += " GROUP BY LOWER(cube) HAVING COUNT(*) >= 5"
                cube_query = text(cube_query_text)
                
                cube_params = {"pattern": pattern, "start_date": start_date, "guild_id": guild_id}                
                cube_result = await session.execute(cube_query, cube_params)
                cube_data = cube_result.fetchall()
                
                # For each cube, get matches played and won
                cube_stats = {}
                for cube_name, draft_count in cube_data:
                    if not cube_name:  # Skip if cube name is None
                        continue
                        
                    # Normalize cube name
                    normalized_cube_name = cube_name.lower()
                    
                    # Get matches played for this cube - update for only completed random drafts
                    cube_matches_query = text("""
                        SELECT COUNT(*) 
                        FROM match_results 
                        WHERE (player1_id = :user_id OR player2_id = :user_id)
                        AND winner_id IS NOT NULL
                        AND session_id IN (
                            SELECT session_id FROM draft_sessions 
                            WHERE LOWER(cube) = :cube_name
                            AND teams_start_time >= :start_date
                            AND session_type IN ('random', 'staked')
                            AND victory_message_id_results_channel IS NOT NULL
                        )
                    """)
                    
                    cube_matches_result = await session.execute(
                        cube_matches_query, 
                        {"user_id": user_id, "cube_name": normalized_cube_name, "start_date": start_date}
                    )
                    cube_matches_played = cube_matches_result.scalar() or 0
                    
                    # Get matches won for this cube - update for only completed random drafts
                    cube_wins_query = text("""
                        SELECT COUNT(*) 
                        FROM match_results 
                        WHERE winner_id = :user_id
                        AND session_id IN (
                            SELECT session_id FROM draft_sessions 
                            WHERE LOWER(cube) = :cube_name
                            AND teams_start_time >= :start_date
                            AND session_type IN ('random', 'staked')
                            AND victory_message_id_results_channel IS NOT NULL
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
                    "display_name": display_name,
                    "cube_stats": cube_stats,
                    # Add team draft stats
                    "team_drafts_played": team_drafts_played,
                    "team_drafts_won": team_drafts_won,
                    "team_drafts_tied": team_drafts_tied,
                    "team_draft_win_percentage": team_draft_win_percentage,
                    # Add streak data
                    "current_win_streak": current_win_streak,
                    "longest_win_streak": longest_win_streak,
                    "current_perfect_streak": current_perfect_streak,
                    "longest_perfect_streak": longest_perfect_streak
                }
                
    except Exception as e:
        logger.error(f"Error getting stats for user {user_id}: {e}")
        # Return default values with team draft stats
        return {
            "drafts_played": 0,
            "matches_played": 0,
            "matches_won": 0,
            "trophies_won": 0,
            "match_win_percentage": 0,
            "current_elo": 1200,
            "display_name": "Unknown",
            "cube_stats": {},
            "team_drafts_played": 0,
            "team_drafts_won": 0,
            "team_drafts_tied": 0,
            "team_draft_win_percentage": 0,
            # Add default streak values
            "current_win_streak": 0,
            "longest_win_streak": 0,
            "current_perfect_streak": 0,
            "longest_perfect_streak": 0
        }
    
async def create_stats_embed(user, stats_weekly, stats_monthly, stats_lifetime):
    """Create a Discord embed with player statistics."""
    # Use the display name from stats if user object doesn't have a name
    display_name = user.display_name if hasattr(user, 'display_name') else stats_lifetime['display_name']
    
    embed = discord.Embed(
        title=f"Stats for {display_name}",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    
    # Set thumbnail if user object is available
    if hasattr(user, 'display_avatar'):
        embed.set_thumbnail(url=user.display_avatar.url)
    
    # Calculate losses for each time frame
    weekly_losses = stats_weekly['team_drafts_played'] - stats_weekly['team_drafts_won'] - stats_weekly['team_drafts_tied']
    monthly_losses = stats_monthly['team_drafts_played'] - stats_monthly['team_drafts_won'] - stats_monthly['team_drafts_tied']
    lifetime_losses = stats_lifetime['team_drafts_played'] - stats_lifetime['team_drafts_won'] - stats_lifetime['team_drafts_tied']
    
    # Weekly stats
    embed.add_field(
        name="Weekly Stats (Last 7 Days)",
        value=(
            f"Drafts Played: {stats_weekly['drafts_played']}\n"
            f"Matches Won: {stats_weekly['matches_won']}/{stats_weekly['matches_played']}\n"
            f"Win %: {stats_weekly['match_win_percentage']:.1f}%\n"
            f"Trophies: {stats_weekly['trophies_won']}\n"
            f"Draft Record: {stats_weekly['team_drafts_won']}-{weekly_losses}-{stats_weekly['team_drafts_tied']}"
            + (f" (Win %: {stats_weekly['team_draft_win_percentage']:.1f}%)" if stats_weekly['team_drafts_played'] > 0 else "")
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
            f"Trophies: {stats_monthly['trophies_won']}\n"
            f"Draft Record: {stats_monthly['team_drafts_won']}-{monthly_losses}-{stats_monthly['team_drafts_tied']}" 
            + (f" (Win %: {stats_monthly['team_draft_win_percentage']:.1f}%)" if stats_monthly['team_drafts_played'] > 0 else "")
        ),
        inline=True
    )
    
    # Lifetime stats
    lifetime_value = (
        f"Drafts Played: {stats_lifetime['drafts_played']}\n"
        f"Matches Won: {stats_lifetime['matches_won']}/{stats_lifetime['matches_played']}\n"
        f"Win %: {stats_lifetime['match_win_percentage']:.1f}%\n"
        f"Trophies: {stats_lifetime['trophies_won']}\n"
    #    f"Current ELO: {stats_lifetime['current_elo']:.0f}\n"
        f"Draft Record: {stats_lifetime['team_drafts_won']}-{lifetime_losses}-{stats_lifetime['team_drafts_tied']}"
        + (f" (Win %: {stats_lifetime['team_draft_win_percentage']:.1f}%)" if stats_lifetime['team_drafts_played'] > 0 else "")
        + "\n\n**Streaks:**\n"
    )

    # Add win streak info
    if stats_lifetime['current_win_streak'] > 0:
        lifetime_value += f"🔥 Current Win Streak: **{stats_lifetime['current_win_streak']}**\n"
    else:
        lifetime_value += f"Current Win Streak: {stats_lifetime['current_win_streak']}\n"

    lifetime_value += f"Longest Win Streak: {stats_lifetime['longest_win_streak']}\n"

    # Add perfect streak info
    if stats_lifetime['current_perfect_streak'] > 0:
        lifetime_value += f"🔥🔥 Current Perfect Streak: **{stats_lifetime['current_perfect_streak']}**\n"
    else:
        lifetime_value += f"Current Perfect Streak: {stats_lifetime['current_perfect_streak']}\n"

    lifetime_value += f"Longest Perfect Streak: {stats_lifetime['longest_perfect_streak']}"

    embed.add_field(
        name="Lifetime Stats",
        value=lifetime_value,
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
            name="Cube Win Percentage (min 5 drafts)",
            value=cube_stats_text,
            inline=False
        )
    
    embed.set_footer(text="Stats are updated after each draft")
    
    return embed


async def get_head_to_head_stats(user1_id, user2_id, user1_display_name=None, user2_display_name=None, guild_id=None):
    """Get head-to-head match statistics between two players."""
    try:
        # Calculate time frames
        now = datetime.now()
        week_ago = now - timedelta(days=7)
        month_ago = now - timedelta(days=30)
        
        # Default values
        weekly_stats = {"matches_played": 0, "user1_wins": 0, "user2_wins": 0}
        monthly_stats = {"matches_played": 0, "user1_wins": 0, "user2_wins": 0}
        lifetime_stats = {"matches_played": 0, "user1_wins": 0, "user2_wins": 0}
        
        # Stats for when users are on opposing teams
        opposing_weekly = {"wins": 0, "losses": 0, "draws": 0, "win_percentage": 0}
        opposing_monthly = {"wins": 0, "losses": 0, "draws": 0, "win_percentage": 0}
        opposing_lifetime = {"wins": 0, "losses": 0, "draws": 0, "win_percentage": 0}
        
        # Stats for when users are on the same team
        teammate_weekly = {"wins": 0, "losses": 0, "draws": 0, "win_percentage": 0}
        teammate_monthly = {"wins": 0, "losses": 0, "draws": 0, "win_percentage": 0}
        teammate_lifetime = {"wins": 0, "losses": 0, "draws": 0, "win_percentage": 0}
        
        # Get display names if not provided
        if not user1_display_name or not user2_display_name:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    # Get player stats for display names
                    if not user1_display_name:
                        player1_query = select(PlayerStats).where(PlayerStats.player_id == user1_id)
                        if guild_id:
                            player1_query = player1_query.where(PlayerStats.guild_id == guild_id)
                        player1_result = await session.execute(player1_query)
                        player1_stats = player1_result.scalar_one_or_none()
                        user1_display_name = player1_stats.display_name if player1_stats else "Unknown"
                    
                    if not user2_display_name:
                        player2_query = select(PlayerStats).where(PlayerStats.player_id == user2_id)
                        if guild_id:
                            player2_query = player2_query.where(PlayerStats.guild_id == guild_id)
                        player2_result = await session.execute(player2_query)
                        player2_stats = player2_result.scalar_one_or_none()
                        user2_display_name = player2_stats.display_name if player2_stats else "Unknown"
                        
                    # If still don't have names, search in sign_ups
                    if user1_display_name == "Unknown" or user2_display_name == "Unknown":
                        # Find in recent drafts
                        recent_drafts_query = select(DraftSession).order_by(DraftSession.teams_start_time.desc())
                        if guild_id:
                            recent_drafts_query = recent_drafts_query.where(DraftSession.guild_id == guild_id)
                        recent_drafts_query = recent_drafts_query.limit(50)
                        recent_drafts_result = await session.execute(recent_drafts_query)
                        recent_drafts = recent_drafts_result.scalars().all()
                        
                        for draft in recent_drafts:
                            if not draft.sign_ups:
                                continue
                            
                            # Process sign_ups
                            sign_ups = draft.sign_ups
                            
                            if user1_display_name == "Unknown" and user1_id in sign_ups:
                                user1_display_name = sign_ups[user1_id]
                            
                            if user2_display_name == "Unknown" and user2_id in sign_ups:
                                user2_display_name = sign_ups[user2_id]
                            
                            # Break if we have both names
                            if user1_display_name != "Unknown" and user2_display_name != "Unknown":
                                break
        
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Find all completed drafts
                drafts_query = select(DraftSession).where(
                    DraftSession.victory_message_id_results_channel.isnot(None)  # Completed drafts
                )
                
                # Add guild_id filter if provided
                if guild_id:
                    drafts_query = drafts_query.where(DraftSession.guild_id == guild_id)
                
                drafts_result = await session.execute(drafts_query)
                all_drafts = drafts_result.scalars().all()
                
                # Process each draft to find ones where both players participated
                for draft in all_drafts:
                    # Skip if no sign_ups
                    if not draft.sign_ups:
                        continue
                    
                    # Skip if either player didn't participate
                    if user1_id not in draft.sign_ups or user2_id not in draft.sign_ups:
                        continue
                    
                    # Skip if no draft date
                    draft_date = draft.teams_start_time
                    if not draft_date:
                        continue
                    
                    # Check if both players have head-to-head matches
                    match_query = select(MatchResult).where(
                        MatchResult.session_id == draft.session_id,
                        or_(
                            and_(MatchResult.player1_id == user1_id, MatchResult.player2_id == user2_id),
                            and_(MatchResult.player1_id == user2_id, MatchResult.player2_id == user1_id)
                        )
                    )
                    match_results = await session.execute(match_query)
                    h2h_matches = match_results.scalars().all()
                    
                    # Process head-to-head matches
                    for match in h2h_matches:
                        if not match.winner_id:
                            continue
                        
                        # Determine match winner in relation to user1
                        user1_won = match.winner_id == user1_id
                        
                        # Update lifetime stats
                        lifetime_stats["matches_played"] += 1
                        if user1_won:
                            lifetime_stats["user1_wins"] += 1
                        else:
                            lifetime_stats["user2_wins"] += 1
                        
                        # Update monthly stats if within last 30 days
                        if draft_date >= month_ago:
                            monthly_stats["matches_played"] += 1
                            if user1_won:
                                monthly_stats["user1_wins"] += 1
                            else:
                                monthly_stats["user2_wins"] += 1
                            
                            # Update weekly stats if within last 7 days
                            if draft_date >= week_ago:
                                weekly_stats["matches_played"] += 1
                                if user1_won:
                                    weekly_stats["user1_wins"] += 1
                                else:
                                    weekly_stats["user2_wins"] += 1
                    
                    # Check team assignments for draft records
                    # Handle potential None values in team_a and team_b safely
                    team_a = draft.team_a or []
                    team_b = draft.team_b or []
                    
                    # Skip if teams aren't fully populated (needed for determining team results)
                    if not team_a or not team_b:
                        continue
                    
                    # Get team win information
                    team_a_wins = 0
                    team_b_wins = 0
                    
                    # Get match results for this draft to calculate team wins
                    all_matches_query = select(MatchResult).where(
                        MatchResult.session_id == draft.session_id,
                        MatchResult.winner_id.isnot(None)  # Only count completed matches
                    )
                    all_match_results = await session.execute(all_matches_query)
                    all_matches = all_match_results.scalars().all()
                    
                    # Count team wins based on which team the winner was on
                    for match in all_matches:
                        if match.winner_id in team_a:
                            team_a_wins += 1
                        elif match.winner_id in team_b:
                            team_b_wins += 1
                    
                    # Determine if players were on the same team or opposing teams
                    user1_in_team_a = user1_id in team_a
                    user1_in_team_b = user1_id in team_b
                    user2_in_team_a = user2_id in team_a
                    user2_in_team_b = user2_id in team_b
                    
                    same_team = (user1_in_team_a and user2_in_team_a) or (user1_in_team_b and user2_in_team_b)
                    
                    # Determine if the draft was a win, loss, or draw for the appropriate set of stats
                    if team_a_wins > team_b_wins:
                        winner_team = "A"
                    elif team_b_wins > team_a_wins:
                        winner_team = "B"
                    else:
                        winner_team = "Draw"
                    
                    if same_team:
                        # Determine which team they were both on
                        their_team = "A" if (user1_in_team_a and user2_in_team_a) else "B"
                        
                        # Update stats based on result
                        stats_list = [teammate_lifetime]
                        if draft_date >= month_ago:
                            stats_list.append(teammate_monthly)
                            if draft_date >= week_ago:
                                stats_list.append(teammate_weekly)
                        
                        for stats in stats_list:
                            if winner_team == "Draw":
                                stats["draws"] += 1
                            elif winner_team == their_team:
                                stats["wins"] += 1
                            else:
                                stats["losses"] += 1
                    else:
                        # They're on opposite teams
                        # Determine which team user1 was on
                        user1_team = "A" if user1_in_team_a else "B"
                        
                        # Update stats based on result (from user1's perspective)
                        stats_list = [opposing_lifetime]
                        if draft_date >= month_ago:
                            stats_list.append(opposing_monthly)
                            if draft_date >= week_ago:
                                stats_list.append(opposing_weekly)
                        
                        for stats in stats_list:
                            if winner_team == "Draw":
                                stats["draws"] += 1
                            elif winner_team == user1_team:
                                stats["wins"] += 1
                            else:
                                stats["losses"] += 1
                
                # Calculate win percentages for match stats
                for stats in [weekly_stats, monthly_stats, lifetime_stats]:
                    stats["user1_win_percentage"] = (stats["user1_wins"] / stats["matches_played"] * 100) if stats["matches_played"] > 0 else 0
                    stats["user2_win_percentage"] = (stats["user2_wins"] / stats["matches_played"] * 100) if stats["matches_played"] > 0 else 0
                
                # Calculate win percentages for team stats (excluding draws from calculation)
                for stats in [opposing_weekly, opposing_monthly, opposing_lifetime, teammate_weekly, teammate_monthly, teammate_lifetime]:
                    wins_plus_losses = stats["wins"] + stats["losses"]
                    if wins_plus_losses > 0:
                        stats["win_percentage"] = (stats["wins"] / wins_plus_losses) * 100
                    else:
                        stats["win_percentage"] = 0
                
                # Debug log to verify data
                logger.info(f"User1_id: {user1_id}, User2_id: {user2_id}")
                logger.info(f"Opposing Lifetime: {opposing_lifetime}")
                logger.info(f"Teammate Lifetime: {teammate_lifetime}")
                
                return {
                    "user1_id": user1_id,
                    "user2_id": user2_id,
                    "user1_display_name": user1_display_name,
                    "user2_display_name": user2_display_name,
                    "weekly": weekly_stats,
                    "monthly": monthly_stats,
                    "lifetime": lifetime_stats,
                    "opposing_weekly": opposing_weekly,
                    "opposing_monthly": opposing_monthly,
                    "opposing_lifetime": opposing_lifetime,
                    "teammate_weekly": teammate_weekly,
                    "teammate_monthly": teammate_monthly,
                    "teammate_lifetime": teammate_lifetime
                }
                
    except Exception as e:
        logger.error(f"Error getting head-to-head stats between {user1_id} and {user2_id}: {e}")
        # Return default values with percentages explicitly set to zero
        weekly_stats = {"matches_played": 0, "user1_wins": 0, "user2_wins": 0, "user1_win_percentage": 0, "user2_win_percentage": 0}
        monthly_stats = {"matches_played": 0, "user1_wins": 0, "user2_wins": 0, "user1_win_percentage": 0, "user2_win_percentage": 0}
        lifetime_stats = {"matches_played": 0, "user1_wins": 0, "user2_wins": 0, "user1_win_percentage": 0, "user2_win_percentage": 0}
        
        # Default values for team records
        empty_stats = {"wins": 0, "losses": 0, "draws": 0, "win_percentage": 0}
        
        return {
            "user1_id": user1_id,
            "user2_id": user2_id,
            "user1_display_name": user1_display_name or "Unknown",
            "user2_display_name": user2_display_name or "Unknown",
            "weekly": weekly_stats,
            "monthly": monthly_stats,
            "lifetime": lifetime_stats,
            "opposing_weekly": empty_stats,
            "opposing_monthly": empty_stats,
            "opposing_lifetime": empty_stats,
            "teammate_weekly": empty_stats,
            "teammate_monthly": empty_stats,
            "teammate_lifetime": empty_stats
        }


async def create_head_to_head_embed(user1, user2, h2h_stats):
    """Create a Discord embed with head-to-head statistics."""
    embed = discord.Embed(
        title=f"{h2h_stats['user1_display_name']} vs {h2h_stats['user2_display_name']}",
        color=discord.Color.gold(),
        timestamp=datetime.now()
    )
    

    # Set the opponent (user2) as the thumbnail
    if user2 and hasattr(user2, 'display_avatar') and user2.display_avatar:
        embed.set_thumbnail(url=user2.display_avatar.url)
    
    # Lifetime stats
    lifetime = h2h_stats.get('lifetime', {"matches_played": 0, "user1_wins": 0, "user2_wins": 0, "user1_win_percentage": 0, "user2_win_percentage": 0})
    embed.add_field(
        name="Match Record",
        value=(
            f"Matches: {lifetime.get('matches_played', 0)}\n"
            f"{h2h_stats['user1_display_name']}: {lifetime.get('user1_wins', 0)} ({lifetime.get('user1_win_percentage', 0):.1f}%)\n"
            f"{h2h_stats['user2_display_name']}: {lifetime.get('user2_wins', 0)} ({lifetime.get('user2_win_percentage', 0):.1f}%)"
        ),
        inline=False
    )
    
    # Monthly stats
    monthly = h2h_stats.get('monthly', {"matches_played": 0, "user1_wins": 0, "user2_wins": 0, "user1_win_percentage": 0, "user2_win_percentage": 0})
    if monthly.get('matches_played', 0) > 0:
        embed.add_field(
            name="Last 30 Days",
            value=(
                f"Matches: {monthly.get('matches_played', 0)}\n"
                f"{h2h_stats['user1_display_name']}: {monthly.get('user1_wins', 0)} ({monthly.get('user1_win_percentage', 0):.1f}%)\n"
                f"{h2h_stats['user2_display_name']}: {monthly.get('user2_wins', 0)} ({monthly.get('user2_win_percentage', 0):.1f}%)"
            ),
            inline=True
        )
    
    # Weekly stats
    weekly = h2h_stats.get('weekly', {"matches_played": 0, "user1_wins": 0, "user2_wins": 0, "user1_win_percentage": 0, "user2_win_percentage": 0})
    if weekly.get('matches_played', 0) > 0:
        embed.add_field(
            name="Last 7 Days",
            value=(
                f"Matches: {weekly.get('matches_played', 0)}\n"
                f"{h2h_stats['user1_display_name']}: {weekly.get('user1_wins', 0)} ({weekly.get('user1_win_percentage', 0):.1f}%)\n"
                f"{h2h_stats['user2_display_name']}: {weekly.get('user2_wins', 0)} ({weekly.get('user2_win_percentage', 0):.1f}%)"
            ),
            inline=True
        )
    
    # Add Draft Record (As Opponents)
    opposing_lifetime = h2h_stats.get('opposing_lifetime', {"wins": 0, "losses": 0, "draws": 0, "win_percentage": 0})
    opposing_monthly = h2h_stats.get('opposing_monthly', {"wins": 0, "losses": 0, "draws": 0, "win_percentage": 0})
    opposing_weekly = h2h_stats.get('opposing_weekly', {"wins": 0, "losses": 0, "draws": 0, "win_percentage": 0})
    
    # Only add the field if there's opponent data
    if opposing_lifetime.get('wins', 0) + opposing_lifetime.get('losses', 0) + opposing_lifetime.get('draws', 0) > 0:
        opp_value = []
        
        if opposing_weekly.get('wins', 0) + opposing_weekly.get('losses', 0) + opposing_weekly.get('draws', 0) > 0:
            opp_value.append(f"Last 7 Days: {opposing_weekly.get('wins', 0)}-{opposing_weekly.get('losses', 0)}-{opposing_weekly.get('draws', 0)} ({opposing_weekly.get('win_percentage', 0):.1f}%)")
            
        if opposing_monthly.get('wins', 0) + opposing_monthly.get('losses', 0) + opposing_monthly.get('draws', 0) > 0:
            opp_value.append(f"Last 30 Days: {opposing_monthly.get('wins', 0)}-{opposing_monthly.get('losses', 0)}-{opposing_monthly.get('draws', 0)} ({opposing_monthly.get('win_percentage', 0):.1f}%)")
            
        opp_value.append(f"Lifetime: {opposing_lifetime.get('wins', 0)}-{opposing_lifetime.get('losses', 0)}-{opposing_lifetime.get('draws', 0)} ({opposing_lifetime.get('win_percentage', 0):.1f}%)")
        
        embed.add_field(
            name="Draft Record (As Opponents)",
            value="\n".join(opp_value),
            inline=False
        )
    
    # Add Draft Record (As Teammates)
    teammate_lifetime = h2h_stats.get('teammate_lifetime', {"wins": 0, "losses": 0, "draws": 0, "win_percentage": 0})
    teammate_monthly = h2h_stats.get('teammate_monthly', {"wins": 0, "losses": 0, "draws": 0, "win_percentage": 0})
    teammate_weekly = h2h_stats.get('teammate_weekly', {"wins": 0, "losses": 0, "draws": 0, "win_percentage": 0})
    
    # Only add the field if there's teammate data
    if teammate_lifetime.get('wins', 0) + teammate_lifetime.get('losses', 0) + teammate_lifetime.get('draws', 0) > 0:
        team_value = []
        
        if teammate_weekly.get('wins', 0) + teammate_weekly.get('losses', 0) + teammate_weekly.get('draws', 0) > 0:
            team_value.append(f"Last 7 Days: {teammate_weekly.get('wins', 0)}-{teammate_weekly.get('losses', 0)}-{teammate_weekly.get('draws', 0)} ({teammate_weekly.get('win_percentage', 0):.1f}%)")
            
        if teammate_monthly.get('wins', 0) + teammate_monthly.get('losses', 0) + teammate_monthly.get('draws', 0) > 0:
            team_value.append(f"Last 30 Days: {teammate_monthly.get('wins', 0)}-{teammate_monthly.get('losses', 0)}-{teammate_monthly.get('draws', 0)} ({teammate_monthly.get('win_percentage', 0):.1f}%)")
            
        team_value.append(f"Lifetime: {teammate_lifetime.get('wins', 0)}-{teammate_lifetime.get('losses', 0)}-{teammate_lifetime.get('draws', 0)} ({teammate_lifetime.get('win_percentage', 0):.1f}%)")
        
        embed.add_field(
            name="Draft Record (As Teammates)",
            value="\n".join(team_value),
            inline=False
        )
    
    embed.set_footer(text="Stats are updated after each match")
    
    return embed

async def find_discord_id_by_display_name_fuzzy(display_name, guild_id=None):
    """
    Find Discord user IDs by partial display name matching.
    
    Args:
        display_name: Partial or full display name to search for
        guild_id: Optional guild ID to filter results
        
    Returns:
        Tuple of (result, name, multiple_matches) where:
        - If multiple_matches is False, result is a single user_id and name is their display_name
        - If multiple_matches is True, result is a list of (user_id, display_name) tuples and name is None
    """
    try:
        matches = []
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # First, check the PlayerStats table for partial matches
                player_query = select(PlayerStats).where(
                    func.lower(PlayerStats.display_name).like(f"%{display_name.lower()}%")
                )
                
                # Add guild_id filter if provided
                if guild_id:
                    player_query = player_query.where(PlayerStats.guild_id == guild_id)
                    
                player_result = await session.execute(player_query)
                players = player_result.scalars().all()
                
                # Add all database matches
                for player in players:
                    matches.append((player.player_id, player.display_name))
                
                # If none found in database, query recent drafts
                if not matches:
                    recent_drafts_query = select(DraftSession).order_by(DraftSession.teams_start_time.desc())
                    
                    # Add guild_id filter if provided
                    if guild_id:
                        recent_drafts_query = recent_drafts_query.where(DraftSession.guild_id == guild_id)
                        
                    recent_drafts_query = recent_drafts_query.limit(100)
                    recent_drafts_result = await session.execute(recent_drafts_query)
                    recent_drafts = recent_drafts_result.scalars().all()
                    
                    # Keep track of all seen display names to avoid duplicates
                    seen_display_names = set()
                    
                    # Search through sign_ups in recent drafts
                    for draft in recent_drafts:
                        if not draft.sign_ups:
                            continue
                        
                        # Process sign_ups
                        sign_ups = draft.sign_ups
                        
                        # Search for partial matches in display_names (case-insensitive)
                        for user_id, user_display_name in sign_ups.items():
                            if isinstance(user_display_name, str) and display_name.lower() in user_display_name.lower():
                                if user_display_name not in seen_display_names:
                                    matches.append((user_id, user_display_name))
                                    seen_display_names.add(user_display_name)
                
                # Check for exact match first (prioritize exact matches)
                for user_id, user_display_name in matches:
                    if user_display_name.lower() == display_name.lower():
                        return user_id, user_display_name, False
                
                # Return results based on number of matches
                if len(matches) == 1:
                    # Single match - return just the ID and display_name 
                    return matches[0][0], matches[0][1], False
                elif len(matches) > 1:
                    # Multiple matches - return the list with a flag
                    return matches, None, True
                else:
                    # No matches
                    return None, None, False
                
    except Exception as e:
        logger.error(f"Error finding Discord ID for display name {display_name}: {e}")
        return None, None, False