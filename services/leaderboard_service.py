import json
from datetime import datetime, timedelta
from loguru import logger
from sqlalchemy import text, select, bindparam
from database.db_session import db_session
from models.win_streak_history import WinStreakHistory
from models.player import PlayerStats

# Win Streak minimum requirements by timeframe
STREAK_MINIMUMS = {
    'active': 6,
    '30d': 6,
    '90d': 8,
    'lifetime': 10
}

def ensure_datetime(date_value):
    """Convert various date formats to datetime objects"""
    if not date_value:
        return None
    
    if isinstance(date_value, datetime):
        return date_value
    
    if isinstance(date_value, str):
        try:
            # Handle ISO format
            if 'T' in date_value:
                # Replace Z with +00:00 for UTC compatibility
                date_value = date_value.replace('Z', '+00:00')
                return datetime.fromisoformat(date_value)
            
            # Handle other common formats
            formats_to_try = [
                '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%d',
                '%m/%d/%Y %H:%M:%S',
                '%m/%d/%Y'
            ]
            
            for date_format in formats_to_try:
                try:
                    return datetime.strptime(date_value, date_format)
                except ValueError:
                    continue
        except Exception as e:
            logger.error(f"Error converting date string '{date_value}': {e}")
    
    # If all else fails
    return None

def get_timeframe_date(timeframe):
    """Get the start date for a given timeframe"""
    now = datetime.now()
    
    if timeframe == "7d":
        return now - timedelta(days=7)
    elif timeframe == "14d":
        return now - timedelta(days=14)
    elif timeframe == "30d":
        return now - timedelta(days=30)
    elif timeframe == "90d":
        return now - timedelta(days=90)
    else:  # "lifetime" or any unrecognized value
        return None  # No date filtering for lifetime

def get_minimum_requirements(timeframe):
    """Get minimum requirements based on timeframe"""
    if timeframe == "14d":
        return {
            "drafts": 5,
            "matches": 12,
            "partnership_drafts": 3
        }
    elif timeframe == "30d":
        return {
            "drafts": 8,
            "matches": 20,
            "partnership_drafts": 3
        }
    elif timeframe == "90d":
        return {
            "drafts": 15,
            "matches": 35,
            "partnership_drafts": 5
        }
    else:  # "lifetime" or any unrecognized value
        return {
            "drafts": 20,
            "matches": 45,
            "partnership_drafts": 8
        }

async def get_leaderboard_data(guild_id, category="draft_record", limit=20, timeframe="lifetime"):
    """Get leaderboard data for all players in a guild"""

    # Get the start date for filtering based on timeframe
    start_date = get_timeframe_date(timeframe)
    
    # Store player stats here
    players_data = {}
    
    async with db_session() as session:
        # Update query to fetch teams_start_time instead of draft_start_time
        drafts_query_text = """
            SELECT id, session_id, team_a, team_b, sign_ups, teams_start_time
            FROM draft_sessions 
            WHERE session_type IN ('random', 'staked')
            AND victory_message_id_results_channel IS NOT NULL
            AND guild_id = :guild_id
        """
        
        # Add timeframe filter if not lifetime
        params = {"guild_id": guild_id}
        if start_date:
            drafts_query_text += " AND teams_start_time >= :start_date"
            params["start_date"] = start_date
            
        drafts_query = text(drafts_query_text)
        drafts_result = await session.execute(drafts_query, params)
        all_drafts = drafts_result.fetchall()
        logger.info(f"Found {len(all_drafts)} completed drafts in guild {guild_id} for timeframe {timeframe}")
        
        # Get match results for these drafts
        if all_drafts:
            # Extract session IDs
            session_ids = [draft[1] for draft in all_drafts]  # session_id is at index 1
            
            # Use proper parameter binding for IN clause with dynamic placeholders
            if len(session_ids) > 0:
                # Create placeholders for the IN clause
                placeholders = ', '.join([f':id{i}' for i in range(len(session_ids))])
                match_results_query_text = f"""
                    SELECT session_id, player1_id, player2_id, winner_id
                    FROM match_results
                    WHERE session_id IN ({placeholders})
                """
                
                # Create parameters dictionary
                params = {f'id{i}': session_id for i, session_id in enumerate(session_ids)}
                
                match_results_query = text(match_results_query_text)
                match_results_result = await session.execute(match_results_query, params)
                all_matches = match_results_result.fetchall()
            else:
                all_matches = []
        else:
            all_matches = []
        
        # Organize match results by session
        match_results_by_session = {}
        for session_id, p1_id, p2_id, winner_id in all_matches:
            if session_id not in match_results_by_session:
                match_results_by_session[session_id] = []
            match_results_by_session[session_id].append((p1_id, p2_id, winner_id))
        
        # Process all drafts and build player data
        for draft_id, session_id, team_a_json, team_b_json, sign_ups_json, teams_start_time in all_drafts:
            try:
                # Parse sign_ups
                sign_ups = json.loads(sign_ups_json) if isinstance(sign_ups_json, str) else sign_ups_json or {}
                
                # Parse teams for win/loss calculations
                team_a = json.loads(team_a_json) if isinstance(team_a_json, str) else team_a_json or []
                team_b = json.loads(team_b_json) if isinstance(team_b_json, str) else team_b_json or []
                
                # Get match results for this session
                session_matches = match_results_by_session.get(session_id, [])
                
                # Calculate team wins for determining draft outcome
                team_a_wins = sum(1 for _, _, winner_id in session_matches if winner_id in team_a)
                team_b_wins = sum(1 for _, _, winner_id in session_matches if winner_id in team_b)
                
                # Process each player in sign_ups
                for player_id, display_name in sign_ups.items():
                    # Initialize player data if not exists
                    if player_id not in players_data:
                        players_data[player_id] = {
                            "player_id": player_id,
                            "display_name": display_name,
                            "drafts_played": 0,
                            "completed_matches": 0,  # Only count matches with a result
                            "matches_won": 0,
                            "matches_lost": 0,
                            "match_win_percentage": 0,
                            "team_drafts_played": 0,
                            "team_drafts_won": 0,
                            "team_drafts_tied": 0,
                            "team_drafts_lost": 0,
                            "team_draft_win_percentage": 0,
                            "teammate_win_rates": {}
                        }
                    else:
                        # Update display name if needed
                        players_data[player_id]["display_name"] = display_name
                    
                    # Update drafts played count
                    players_data[player_id]["drafts_played"] += 1
                    
                    # Update individual matches played/won
                    for p1_id, p2_id, winner_id in session_matches:
                        if p1_id == player_id or p2_id == player_id:
                            # Only count matches that have a determined winner
                            if winner_id is not None:
                                players_data[player_id]["completed_matches"] += 1
                                
                                if winner_id == player_id:
                                    players_data[player_id]["matches_won"] += 1
                                else:
                                    # Explicitly count losses when the winner is not the player
                                    players_data[player_id]["matches_lost"] += 1
                    
                    # Update team draft stats
                    if player_id in team_a or player_id in team_b:
                        player_team = "A" if player_id in team_a else "B"
                        player_teammates = team_a if player_id in team_a else team_b
                        
                        # Determine match outcome
                        if team_a_wins > team_b_wins:
                            winner_team = "A"
                        elif team_b_wins > team_a_wins:
                            winner_team = "B"
                        else:
                            winner_team = "Draw"
                        
                        # Update team stats
                        players_data[player_id]["team_drafts_played"] += 1
                        
                        # Determine draft result based on winner_team
                        if winner_team == "Draw":
                            players_data[player_id]["team_drafts_tied"] += 1
                        elif winner_team == player_team:
                            players_data[player_id]["team_drafts_won"] += 1
                        else:
                            players_data[player_id]["team_drafts_lost"] += 1
                        
                        # Update teammate stats
                        for teammate_id in player_teammates:
                            if teammate_id != player_id and teammate_id in players_data:
                                # Initialize teammate record if not exists
                                if teammate_id not in players_data[player_id]["teammate_win_rates"]:
                                    players_data[player_id]["teammate_win_rates"][teammate_id] = {
                                        "drafts_played": 0,
                                        "drafts_won": 0,
                                        "drafts_lost": 0,
                                        "drafts_tied": 0,
                                        "win_percentage": 0,
                                        "teammate_name": players_data[teammate_id]["display_name"]
                                    }
                                
                                # Update teammate stats based on the match outcome
                                players_data[player_id]["teammate_win_rates"][teammate_id]["drafts_played"] += 1
                                
                                if winner_team == "Draw":
                                    players_data[player_id]["teammate_win_rates"][teammate_id]["drafts_tied"] += 1
                                elif winner_team == player_team:
                                    players_data[player_id]["teammate_win_rates"][teammate_id]["drafts_won"] += 1
                                else:
                                    players_data[player_id]["teammate_win_rates"][teammate_id]["drafts_lost"] += 1
            
            except Exception as e:
                logger.error(f"Error processing draft {draft_id}: {e}")
        
        # Calculate percentages for each player
        for player_id, player_data in players_data.items():
            # Calculate match win percentage
            if player_data["completed_matches"] > 0:
                player_data["match_win_percentage"] = (player_data["matches_won"] / player_data["completed_matches"]) * 100
            
            # Calculate team draft win percentage
            team_draft_counted_drafts = player_data["team_drafts_won"] + player_data["team_drafts_lost"]
            if team_draft_counted_drafts > 0:
                player_data["team_draft_win_percentage"] = (player_data["team_drafts_won"] / team_draft_counted_drafts) * 100
            
            # Calculate teammate win rates
            for teammate_id, teammate_data in player_data["teammate_win_rates"].items():
                counted_drafts = teammate_data["drafts_won"] + teammate_data["drafts_lost"]
                if counted_drafts > 0:
                    teammate_data["win_percentage"] = (teammate_data["drafts_won"] / counted_drafts) * 100

        # Convert to list for sorting
        players_list = list(players_data.values())

        # Get minimum requirements based on timeframe
        min_requirements = get_minimum_requirements(timeframe)
        min_drafts = min_requirements["drafts"]
        min_matches = min_requirements["matches"]
        min_partnership_drafts = min_requirements["partnership_drafts"]
        
        # Apply category-specific filters and sorting
        if category == "draft_record":
            filtered_players = [p for p in players_list if p["drafts_played"] >= min_drafts and p["team_draft_win_percentage"] >= 50]
            logger.info(f"Found {len(filtered_players)} players with at least {min_drafts} drafts for draft_record")
            # Sort by team draft win percentage (descending)
            sorted_players = sorted(filtered_players, key=lambda p: p["team_draft_win_percentage"], reverse=True)
        
        elif category == "match_win":
            filtered_players = [p for p in players_list if p["completed_matches"] >= min_matches and p["match_win_percentage"] >= 50]
            logger.info(f"Found {len(filtered_players)} players with at least {min_matches} completed matches for match_win")
            # Sort by match win percentage (descending)
            sorted_players = sorted(filtered_players, key=lambda p: p["match_win_percentage"], reverse=True)
        
        elif category == "drafts_played":
            # Sort by number of drafts played (descending)
            sorted_players = sorted(players_list, key=lambda p: p["drafts_played"], reverse=True)
        
        elif category == "time_vault_and_key":
            # Process teammate data to find best partnerships
            best_partnerships = []
            total_relationships = 0
            seen_pairs = set()  # Track unique pairs

            for player_id, player_data in players_data.items():
                total_relationships += len(player_data["teammate_win_rates"])
                
                for teammate_id, teammate_data in player_data["teammate_win_rates"].items():
                    # Create a unique key for the pair (sorted to avoid duplicate direction)
                    pair_key = tuple(sorted([player_id, teammate_id]))
                    if pair_key in seen_pairs:
                        continue  # Skip already processed pair
                    seen_pairs.add(pair_key)

                    counted_drafts = teammate_data["drafts_won"] + teammate_data["drafts_lost"]
                    if counted_drafts >= min_partnership_drafts:
                        win_percentage = (teammate_data["drafts_won"] / counted_drafts) * 100
                        if win_percentage >= 50: 
                            partnership = {
                                "player_id": player_id,
                                "player_name": player_data["display_name"],
                                "teammate_id": teammate_id,
                                "teammate_name": teammate_data["teammate_name"],
                                "drafts_played": teammate_data["drafts_played"],
                                "drafts_won": teammate_data["drafts_won"],
                                "drafts_lost": teammate_data["drafts_lost"],
                                "drafts_tied": teammate_data["drafts_tied"],
                                "win_percentage": win_percentage
                            }

                            best_partnerships.append(partnership)

            logger.info(f"Found {total_relationships} total teammate relationships")
            logger.info(f"Found {len(best_partnerships)} partnerships with at least {min_partnership_drafts} drafts together")
            
            # Sort partnerships by win percentage
            sorted_players = sorted(best_partnerships, key=lambda p: p["win_percentage"], reverse=True)
        
        elif category == "hot_streak":
            # For hot streak, we always use the 7-day timeframe regardless of what was passed
            filtered_players = [p for p in players_list if p["completed_matches"] >= 9 and p["match_win_percentage"] > 50]
            logger.info(f"Found {len(filtered_players)} players with at least 9 completed matches for hot_streak")
            # Sort by match win percentage
            sorted_players = sorted(filtered_players, key=lambda p: p["match_win_percentage"], reverse=True)

        elif category == "longest_win_streak":
            # Use dedicated function for streak queries (doesn't need draft aggregation)
            sorted_players = await get_win_streak_leaderboard_data(guild_id, timeframe, limit, session)

        else:
            # Default to drafts_played if category not recognized
            sorted_players = sorted(players_list, key=lambda p: p["drafts_played"], reverse=True)

        # Limit to requested number
        return sorted_players[:limit]


async def get_win_streak_leaderboard_data(guild_id, timeframe, limit, session):
    """
    Get win streak leaderboard data.
    Separated from main function because it doesn't need draft aggregation.
    """
    min_streak = STREAK_MINIMUMS.get(timeframe, 10)

    # Calculate date cutoff for timeframe
    if timeframe == "active":
        cutoff_date = None  # Show all active streaks
    elif timeframe == "lifetime":
        cutoff_date = None  # No date filter
    elif timeframe == "90d":
        cutoff_date = datetime.now() - timedelta(days=90)
    elif timeframe == "30d":
        cutoff_date = datetime.now() - timedelta(days=30)
    else:
        cutoff_date = None

    # === Part 1: Get completed streaks from history ===
    if timeframe == "active":
        # For "active" timeframe, exclude all completed streaks
        history_streaks = []
    else:
        history_stmt = select(WinStreakHistory).where(
            WinStreakHistory.guild_id == guild_id,
            WinStreakHistory.ended_at.isnot(None)  # Only completed streaks
        )

        if cutoff_date:
            # Streak must have ENDED within timeframe (recently completed)
            history_stmt = history_stmt.where(
                WinStreakHistory.ended_at >= cutoff_date
            )

        history_result = await session.execute(history_stmt)
        history_streaks = history_result.scalars().all()

    # === Part 1.5: Bulk load PlayerStats for all streak players (avoid N+1 queries) ===
    if history_streaks:
        streak_player_ids = list(set(s.player_id for s in history_streaks))
        players_bulk_stmt = select(PlayerStats).where(
            PlayerStats.guild_id == guild_id,
            PlayerStats.player_id.in_(streak_player_ids)
        )
        players_bulk_result = await session.execute(players_bulk_stmt)
        players_bulk = players_bulk_result.scalars().all()
        players_lookup = {p.player_id: p for p in players_bulk}
    else:
        players_lookup = {}

    # === Part 2: Get active streaks from PlayerStats ===
    # Active streaks are always included (they're happening NOW)
    # No date filtering needed - if it's active, it's current
    players_stmt = select(PlayerStats).where(
        PlayerStats.guild_id == guild_id,
        PlayerStats.current_win_streak > 0
    )

    players_result = await session.execute(players_stmt)
    active_players = players_result.scalars().all()

    # === Part 3: Combine into unified format ===
    streak_entries = []

    # Add completed streaks (using bulk-loaded players)
    for streak in history_streaks:
        player = players_lookup.get(streak.player_id)

        if player and streak.streak_length >= min_streak:
            streak_entries.append({
                "player_id": streak.player_id,
                "display_name": player.display_name,
                "longest_win_streak": streak.streak_length,
                "games_won": player.games_won,
                "games_lost": player.games_lost,
                "completed_matches": player.games_won + player.games_lost,
                "is_active": False,
                "started_at": streak.started_at,
                "ended_at": streak.ended_at
            })

    # Add active streaks
    for player in active_players:
        if player.current_win_streak >= min_streak:
            streak_entries.append({
                "player_id": player.player_id,
                "display_name": player.display_name,
                "longest_win_streak": player.current_win_streak,
                "games_won": player.games_won,
                "games_lost": player.games_lost,
                "completed_matches": player.games_won + player.games_lost,
                "is_active": True,
                "started_at": player.current_win_streak_started_at,
                "ended_at": None
            })

    # === Part 4: Deduplicate - keep best per player ===
    player_best_streaks = {}
    for entry in streak_entries:
        player_id = entry["player_id"]
        if player_id not in player_best_streaks:
            player_best_streaks[player_id] = entry
        else:
            # Keep the longer streak
            if entry["longest_win_streak"] > player_best_streaks[player_id]["longest_win_streak"]:
                player_best_streaks[player_id] = entry

    # === Part 5: Sort by streak length, then win % ===
    sorted_players = sorted(
        player_best_streaks.values(),
        key=lambda p: (
            p["longest_win_streak"],
            p["games_won"] / p["completed_matches"] if p["completed_matches"] > 0 else 0
        ),
        reverse=True
    )

    # Apply limit
    return sorted_players[:limit]