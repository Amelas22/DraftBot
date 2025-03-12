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
                
                # Define the pattern for JSON searches
                pattern = f'%"{user_id}"%'  # Pattern to match user_id in JSON string
                
                base_query = """
                    FROM draft_sessions 
                    WHERE (
                        json_extract(sign_ups, '$') LIKE :pattern
                        OR json_extract(team_a, '$') LIKE :pattern
                        OR json_extract(team_b, '$') LIKE :pattern
                    ) 
                    AND draft_start_time >= :start_date
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
                        WHERE draft_start_time >= :start_date
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
                name_query_text += " ORDER BY draft_start_time DESC LIMIT 1"
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
                    AND draft_start_time >= :start_date
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
                        WHERE draft_start_time >= :start_date
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
                    AND draft_start_time >= :start_date
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
                    AND draft_start_time >= :start_date
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
                            AND draft_start_time >= :start_date
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
                            AND draft_start_time >= :start_date
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
                    "team_draft_win_percentage": team_draft_win_percentage
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
            "team_draft_win_percentage": 0
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
    embed.add_field(
        name="Lifetime Stats",
        value=(
            f"Drafts Played: {stats_lifetime['drafts_played']}\n"
            f"Matches Won: {stats_lifetime['matches_won']}/{stats_lifetime['matches_played']}\n"
            f"Win %: {stats_lifetime['match_win_percentage']:.1f}%\n"
            f"Trophies: {stats_lifetime['trophies_won']}\n"
        #    f"Current ELO: {stats_lifetime['current_elo']:.0f}\n"
            f"Draft Record: {stats_lifetime['team_drafts_won']}-{lifetime_losses}-{stats_lifetime['team_drafts_tied']}"
            + (f" (Win %: {stats_lifetime['team_draft_win_percentage']:.1f}%)" if stats_lifetime['team_drafts_played'] > 0 else "")
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
            name="Cube Win Percentage (min 5 drafts)",
            value=cube_stats_text,
            inline=False
        )
    
    embed.set_footer(text="Stats are updated after each draft")
    
    return embed

async def find_discord_id_by_display_name(display_name):
    """Find a Discord user ID from their display name by searching through recent DraftSessions."""
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # First, check the PlayerStats table for an exact match
                player_query = select(PlayerStats).where(func.lower(PlayerStats.display_name) == display_name.lower())
                player_result = await session.execute(player_query)
                player = player_result.scalar_one_or_none()
                
                if player:
                    return player.player_id, player.display_name
                
                # If not found, query for recent DraftSessions
                recent_drafts_query = select(DraftSession).order_by(DraftSession.draft_start_time.desc()).limit(100)
                recent_drafts_result = await session.execute(recent_drafts_query)
                recent_drafts = recent_drafts_result.scalars().all()
                
                # Search through sign_ups in recent drafts
                for draft in recent_drafts:
                    if not draft.sign_ups:
                        continue
                    
                    # Process sign_ups
                    sign_ups = draft.sign_ups
                    
                    # Search for display_name in values (case-insensitive)
                    for user_id, user_display_name in sign_ups.items():
                        if isinstance(user_display_name, str) and user_display_name.lower() == display_name.lower():
                            return user_id, user_display_name
                
                # If we get here, no match was found
                return None, None
                
    except Exception as e:
        logger.error(f"Error finding Discord ID for display name {display_name}: {e}")
        return None, None

async def get_head_to_head_stats(user1_id, user2_id, user1_display_name=None, user2_display_name=None):
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
        
        # Get display names if not provided
        if not user1_display_name or not user2_display_name:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    # Get player stats for display names
                    if not user1_display_name:
                        player1_query = select(PlayerStats).where(PlayerStats.player_id == user1_id)
                        player1_result = await session.execute(player1_query)
                        player1_stats = player1_result.scalar_one_or_none()
                        user1_display_name = player1_stats.display_name if player1_stats else "Unknown"
                    
                    if not user2_display_name:
                        player2_query = select(PlayerStats).where(PlayerStats.player_id == user2_id)
                        player2_result = await session.execute(player2_query)
                        player2_stats = player2_result.scalar_one_or_none()
                        user2_display_name = player2_stats.display_name if player2_stats else "Unknown"
                        
                    # If still don't have names, search in sign_ups
                    if user1_display_name == "Unknown" or user2_display_name == "Unknown":
                        # Find in recent drafts
                        recent_drafts_query = select(DraftSession).order_by(DraftSession.draft_start_time.desc()).limit(50)
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
                # Find all matches where both players participated
                match_query = select(MatchResult).where(
                    or_(
                        and_(MatchResult.player1_id == user1_id, MatchResult.player2_id == user2_id),
                        and_(MatchResult.player1_id == user2_id, MatchResult.player2_id == user1_id)
                    )
                )
                match_results = await session.execute(match_query)
                matches = match_results.scalars().all()
                
                # Get draft sessions for times
                session_ids = [match.session_id for match in matches]
                if session_ids:
                    drafts_query = select(DraftSession).where(DraftSession.session_id.in_(session_ids))
                    drafts_result = await session.execute(drafts_query)
                    drafts = {draft.session_id: draft for draft in drafts_result.scalars().all()}
                else:
                    drafts = {}
                
                # Process each match
                for match in matches:
                    draft = drafts.get(match.session_id)
                    if not draft or not match.winner_id:
                        continue
                        
                    match_date = draft.draft_start_time
                    if not match_date:
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
                    if match_date >= month_ago:
                        monthly_stats["matches_played"] += 1
                        if user1_won:
                            monthly_stats["user1_wins"] += 1
                        else:
                            monthly_stats["user2_wins"] += 1
                        
                        # Update weekly stats if within last 7 days
                        if match_date >= week_ago:
                            weekly_stats["matches_played"] += 1
                            if user1_won:
                                weekly_stats["user1_wins"] += 1
                            else:
                                weekly_stats["user2_wins"] += 1
                
                # Calculate win percentages
                for stats in [weekly_stats, monthly_stats, lifetime_stats]:
                    stats["user1_win_percentage"] = (stats["user1_wins"] / stats["matches_played"] * 100) if stats["matches_played"] > 0 else 0
                    stats["user2_win_percentage"] = (stats["user2_wins"] / stats["matches_played"] * 100) if stats["matches_played"] > 0 else 0
                
                return {
                    "user1_id": user1_id,
                    "user2_id": user2_id,
                    "user1_display_name": user1_display_name,
                    "user2_display_name": user2_display_name,
                    "weekly": weekly_stats,
                    "monthly": monthly_stats,
                    "lifetime": lifetime_stats
                }
                
    except Exception as e:
        logger.error(f"Error getting head-to-head stats between {user1_id} and {user2_id}: {e}")
        # Return default values
        return {
            "user1_id": user1_id,
            "user2_id": user2_id,
            "user1_display_name": user1_display_name or "Unknown",
            "user2_display_name": user2_display_name or "Unknown",
            "weekly": {"matches_played": 0, "user1_wins": 0, "user2_wins": 0, "user1_win_percentage": 0, "user2_win_percentage": 0},
            "monthly": {"matches_played": 0, "user1_wins": 0, "user2_wins": 0, "user1_win_percentage": 0, "user2_win_percentage": 0},
            "lifetime": {"matches_played": 0, "user1_wins": 0, "user2_wins": 0, "user1_win_percentage": 0, "user2_win_percentage": 0}
        }

async def create_head_to_head_embed(user1, user2, h2h_stats):
    """Create a Discord embed with head-to-head statistics."""
    embed = discord.Embed(
        title=f"{h2h_stats['user1_display_name']} vs {h2h_stats['user2_display_name']}",
        color=discord.Color.gold(),
        timestamp=datetime.now()
    )
    
    # Set thumbnails if user objects are available
    if hasattr(user1, 'display_avatar') and user1.display_avatar:
        embed.set_thumbnail(url=user1.display_avatar.url)
    
    if user2 and hasattr(user2, 'display_avatar') and user2.display_avatar:
        embed.set_author(name=user2.display_name, icon_url=user2.display_avatar.url)
    
    # Lifetime stats
    lifetime = h2h_stats['lifetime']
    embed.add_field(
        name="Lifetime Record",
        value=(
            f"Matches: {lifetime['matches_played']}\n"
            f"{h2h_stats['user1_display_name']}: {lifetime['user1_wins']} ({lifetime['user1_win_percentage']:.1f}%)\n"
            f"{h2h_stats['user2_display_name']}: {lifetime['user2_wins']} ({lifetime['user2_win_percentage']:.1f}%)"
        ),
        inline=False
    )
    
    # Monthly stats
    monthly = h2h_stats['monthly']
    if monthly['matches_played'] > 0:
        embed.add_field(
            name="Last 30 Days",
            value=(
                f"Matches: {monthly['matches_played']}\n"
                f"{h2h_stats['user1_display_name']}: {monthly['user1_wins']} ({monthly['user1_win_percentage']:.1f}%)\n"
                f"{h2h_stats['user2_display_name']}: {monthly['user2_wins']} ({monthly['user2_win_percentage']:.1f}%)"
            ),
            inline=True
        )
    
    # Weekly stats
    weekly = h2h_stats['weekly']
    if weekly['matches_played'] > 0:
        embed.add_field(
            name="Last 7 Days",
            value=(
                f"Matches: {weekly['matches_played']}\n"
                f"{h2h_stats['user1_display_name']}: {weekly['user1_wins']} ({weekly['user1_win_percentage']:.1f}%)\n"
                f"{h2h_stats['user2_display_name']}: {weekly['user2_wins']} ({weekly['user2_win_percentage']:.1f}%)"
            ),
            inline=True
        )
    
    embed.set_footer(text="Stats are updated after each match")
    
    return embed