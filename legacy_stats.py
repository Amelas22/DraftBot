import pandas as pd
import os
import json
from datetime import datetime
from sqlalchemy import select
from player_stats import get_head_to_head_stats, get_player_statistics

# Constants
LEGACY_GUILD_ID = "715228693529886760"  
CSV_DIR = "legacy_data"  
MATCH_RESULTS_FILE = os.path.join(CSV_DIR, "matchResults.csv")
DRAFT_RESULTS_FILE = os.path.join(CSV_DIR, "draftResults.csv")

# Cache for legacy data to avoid reprocessing
_legacy_data_cache = None

def load_legacy_data():
    """
    Load and process the legacy CSV data.
    Returns a tuple of (match_results_df, draft_results_df)
    """
    global _legacy_data_cache
    
    # Return cached data if available
    if _legacy_data_cache is not None:
        return _legacy_data_cache
    
    # Ensure the directory exists
    if not os.path.exists(CSV_DIR):
        os.makedirs(CSV_DIR)
    
    # Check if files exist
    if not os.path.exists(MATCH_RESULTS_FILE) or not os.path.exists(DRAFT_RESULTS_FILE):
        print(f"Legacy data files not found in {CSV_DIR}")
        return None, None
    
    # Load CSV files
    try:
        match_results_df = pd.read_csv(MATCH_RESULTS_FILE)
        draft_results_df = pd.read_csv(DRAFT_RESULTS_FILE)
        
        # Filter draft results for the specific guild
        draft_results_df = draft_results_df[draft_results_df['guild_id'] == int(LEGACY_GUILD_ID)]
        
        # Convert date strings to datetime objects
        match_results_df['createdAt'] = pd.to_datetime(match_results_df['createdAt'])
        draft_results_df['createdAt'] = pd.to_datetime(draft_results_df['createdAt'])
        
        # Cache the processed data
        _legacy_data_cache = (match_results_df, draft_results_df)
        
        return match_results_df, draft_results_df
    
    except Exception as e:
        print(f"Error loading legacy data: {e}")
        return None, None

def process_legacy_drafts():
    """
    Process the legacy draft data to create a structured representation.
    Returns a dictionary of processed drafts with team info and match results.
    """
    match_results_df, draft_results_df = load_legacy_data()
    if match_results_df is None or draft_results_df is None:
        return {}
    
    processed_drafts = {}
    
    # Process each draft
    for _, draft in draft_results_df.iterrows():
        draft_id = draft['id']
        
        # Get matches for this draft
        draft_matches = match_results_df[match_results_df['draftResultId'] == draft_id]
        
        # Skip drafts with no matches
        if len(draft_matches) == 0:
            continue
        
        # Initialize team info
        team_blue = set()
        team_red = set()
        team_blue_wins = 0
        team_red_wins = 0
        players_with_matches = set()
        match_details = []
        
        # Process each match in the draft
        for _, match in draft_matches.iterrows():
            blue_player = str(match['bluePlayer'])
            red_player = str(match['redPlayer'])
            result = match['result']
            
            # Add players to their teams
            team_blue.add(blue_player)
            team_red.add(red_player)
            
            # Process match result if it was played
            if result in ['blue', 'red']:
                players_with_matches.add(blue_player)
                players_with_matches.add(red_player)
                
                if result == 'blue':
                    team_blue_wins += 1
                    winner_id = blue_player
                else:  # result == 'red'
                    team_red_wins += 1
                    winner_id = red_player
                
                # Add match details
                match_details.append({
                    'player1_id': blue_player,
                    'player2_id': red_player,
                    'winner_id': winner_id,
                    'timestamp': match['createdAt']
                })
        
        # Only include drafts with played matches
        if len(match_details) > 0:
            # Determine the overall winner
            if team_blue_wins > team_red_wins:
                winning_team = "blue"
            elif team_red_wins > team_blue_wins:
                winning_team = "red"
            else:
                winning_team = "draw"
            
            # Create processed draft entry
            processed_drafts[str(draft_id)] = {
                'draft_id': str(draft_id),
                'team_blue': list(team_blue),
                'team_red': list(team_red),
                'team_blue_wins': team_blue_wins,
                'team_red_wins': team_red_wins,
                'winning_team': winning_team,
                'match_details': match_details,
                'timestamp': draft['createdAt'],
                'guild_id': LEGACY_GUILD_ID,
                'team_formation': draft['team_formation']
            }
    
    return processed_drafts

def get_legacy_player_stats(user_id, time_frame=None):
    """
    Get player statistics from legacy data.
    
    Args:
        user_id: The Discord user ID
        time_frame: 'week', 'month', or None for lifetime
        
    Returns:
        Dictionary with player stats from legacy data
    """
    user_id = str(user_id)
    processed_drafts = process_legacy_drafts()
    
    # Default stats
    stats = {
        'drafts_played': 0,
        'matches_played': 0,
        'matches_won': 0,
        'trophies_won': 0,
        'team_drafts_played': 0,
        'team_drafts_won': 0,
        'team_drafts_tied': 0
    }
    
    if not processed_drafts:
        return stats
    
    # Determine cutoff date based on time frame
    now = datetime.now()
    cutoff_date = None
    if time_frame == 'week':
        cutoff_date = now - pd.Timedelta(days=7)
    elif time_frame == 'month':
        cutoff_date = now - pd.Timedelta(days=30)
    
    # Process each draft
    for draft_id, draft in processed_drafts.items():
        draft_timestamp = draft['timestamp']
        
        # Skip if outside time frame
        if cutoff_date and draft_timestamp < cutoff_date:
            continue
        
        # Check if user was in this draft
        user_in_blue = user_id in draft['team_blue']
        user_in_red = user_id in draft['team_red']
        
        if not (user_in_blue or user_in_red):
            continue
        
        # Count draft
        stats['drafts_played'] += 1
        stats['team_drafts_played'] += 1
        
        # Determine user's team and if they won
        user_team = 'blue' if user_in_blue else 'red'
        
        if draft['winning_team'] == 'draw':
            stats['team_drafts_tied'] += 1
        elif draft['winning_team'] == user_team:
            stats['team_drafts_won'] += 1
            
            # Trophy counts for 3-0 drafters
            if user_team == 'blue' and draft['team_blue_wins'] > 0 and draft['team_red_wins'] == 0:
                stats['trophies_won'] += 1
            elif user_team == 'red' and draft['team_red_wins'] > 0 and draft['team_blue_wins'] == 0:
                stats['trophies_won'] += 1
        
        # Count individual matches
        for match in draft['match_details']:
            if user_id in [match['player1_id'], match['player2_id']]:
                stats['matches_played'] += 1
                if match['winner_id'] == user_id:
                    stats['matches_won'] += 1
    
    # Calculate team win percentage
    if stats['team_drafts_played'] > 0:
        team_games = stats['team_drafts_won'] + (stats['team_drafts_played'] - stats['team_drafts_won'] - stats['team_drafts_tied'])
        if team_games > 0:
            stats['team_draft_win_percentage'] = (stats['team_drafts_won'] / team_games) * 100
        else:
            stats['team_draft_win_percentage'] = 0
    else:
        stats['team_draft_win_percentage'] = 0
    
    # Calculate match win percentage
    if stats['matches_played'] > 0:
        stats['match_win_percentage'] = (stats['matches_won'] / stats['matches_played']) * 100
    else:
        stats['match_win_percentage'] = 0
        
    return stats

def get_legacy_head_to_head_stats(user1_id, user2_id, time_frame=None):
    """
    Get head-to-head statistics from legacy data.
    
    Args:
        user1_id: First player's Discord user ID
        user2_id: Second player's Discord user ID
        time_frame: 'week', 'month', or None for lifetime
        
    Returns:
        Dictionary with head-to-head stats from legacy data
    """
    user1_id = str(user1_id)
    user2_id = str(user2_id)
    processed_drafts = process_legacy_drafts()
    
    # Default stats
    match_stats = {"matches_played": 0, "user1_wins": 0, "user2_wins": 0}
    opposing_stats = {"wins": 0, "losses": 0, "draws": 0}
    teammate_stats = {"wins": 0, "losses": 0, "draws": 0}
    
    if not processed_drafts:
        return {
            "match_stats": match_stats,
            "opposing_stats": opposing_stats,
            "teammate_stats": teammate_stats
        }
    
    # Determine cutoff date based on time frame
    now = datetime.now()
    cutoff_date = None
    if time_frame == 'week':
        cutoff_date = now - pd.Timedelta(days=7)
    elif time_frame == 'month':
        cutoff_date = now - pd.Timedelta(days=30)
    
    # Process each draft
    for draft_id, draft in processed_drafts.items():
        draft_timestamp = draft['timestamp']
        
        # Skip if outside time frame
        if cutoff_date and draft_timestamp < cutoff_date:
            continue
        
        # Check if both users were in this draft
        user1_in_blue = user1_id in draft['team_blue']
        user1_in_red = user1_id in draft['team_red']
        user2_in_blue = user2_id in draft['team_blue']
        user2_in_red = user2_id in draft['team_red']
        
        if not ((user1_in_blue or user1_in_red) and (user2_in_blue or user2_in_red)):
            continue
        
        # Determine if they were teammates or opponents
        same_team = (user1_in_blue and user2_in_blue) or (user1_in_red and user2_in_red)
        
        if same_team:
            # Teammates - determine which team they were both on
            their_team = 'blue' if (user1_in_blue and user2_in_blue) else 'red'
            
            # Update teammate stats
            if draft['winning_team'] == 'draw':
                teammate_stats['draws'] += 1
            elif draft['winning_team'] == their_team:
                teammate_stats['wins'] += 1
            else:
                teammate_stats['losses'] += 1
        else:
            # Opponents - determine user1's team
            user1_team = 'blue' if user1_in_blue else 'red'
            
            # Update opposing stats from user1's perspective
            if draft['winning_team'] == 'draw':
                opposing_stats['draws'] += 1
            elif draft['winning_team'] == user1_team:
                opposing_stats['wins'] += 1
            else:
                opposing_stats['losses'] += 1
        
        # Count head-to-head matches
        for match in draft['match_details']:
            if ((match['player1_id'] == user1_id and match['player2_id'] == user2_id) or
                (match['player1_id'] == user2_id and match['player2_id'] == user1_id)):
                
                match_stats['matches_played'] += 1
                
                if match['winner_id'] == user1_id:
                    match_stats['user1_wins'] += 1
                elif match['winner_id'] == user2_id:
                    match_stats['user2_wins'] += 1
    
    # Calculate win percentages for match stats
    if match_stats['matches_played'] > 0:
        match_stats['user1_win_percentage'] = (match_stats['user1_wins'] / match_stats['matches_played']) * 100
        match_stats['user2_win_percentage'] = (match_stats['user2_wins'] / match_stats['matches_played']) * 100
    else:
        match_stats['user1_win_percentage'] = 0
        match_stats['user2_win_percentage'] = 0
    
    # Calculate win percentages for team stats (excluding draws)
    for stats in [opposing_stats, teammate_stats]:
        wins_plus_losses = stats['wins'] + stats['losses']
        if wins_plus_losses > 0:
            stats['win_percentage'] = (stats['wins'] / wins_plus_losses) * 100
        else:
            stats['win_percentage'] = 0
    
    return {
        "match_stats": match_stats,
        "opposing_stats": opposing_stats,
        "teammate_stats": teammate_stats
    }

# Modified version of get_player_statistics to incorporate legacy data
async def get_player_statistics_with_legacy(user_id, time_frame=None, user_display_name=None, guild_id=None):
    """
    Get player statistics, combining current and legacy data.
    
    Args:
        user_id: The Discord user ID
        time_frame: 'week', 'month', or None for lifetime
        user_display_name: Display name of the user (optional)
        guild_id: Guild ID to filter stats by (optional)
        
    Returns:
        Dictionary with combined player stats
    """
    # Get current stats from the database
    
    current_stats = await get_player_statistics(user_id, time_frame, user_display_name, guild_id)
    
    # Only include legacy data if we're looking at the specific guild
    if guild_id == LEGACY_GUILD_ID:
        # Get legacy stats
        legacy_stats = get_legacy_player_stats(user_id, time_frame)
        
        # Merge the stats
        merged_stats = {
            "drafts_played": current_stats.get("drafts_played", 0) + legacy_stats.get("drafts_played", 0),
            "matches_played": current_stats.get("matches_played", 0) + legacy_stats.get("matches_played", 0),
            "matches_won": current_stats.get("matches_won", 0) + legacy_stats.get("matches_won", 0),
            "trophies_won": current_stats.get("trophies_won", 0) + legacy_stats.get("trophies_won", 0),
            "team_drafts_played": current_stats.get("team_drafts_played", 0) + legacy_stats.get("team_drafts_played", 0),
            "team_drafts_won": current_stats.get("team_drafts_won", 0) + legacy_stats.get("team_drafts_won", 0),
            "team_drafts_tied": current_stats.get("team_drafts_tied", 0) + legacy_stats.get("team_drafts_tied", 0),
            "display_name": current_stats.get("display_name", user_display_name),
            "cube_stats": current_stats.get("cube_stats", {})
        }
        
        # Recalculate percentages
        if merged_stats["matches_played"] > 0:
            merged_stats["match_win_percentage"] = (merged_stats["matches_won"] / merged_stats["matches_played"]) * 100
        else:
            merged_stats["match_win_percentage"] = 0
            
        team_games = merged_stats["team_drafts_won"] + (merged_stats["team_drafts_played"] - merged_stats["team_drafts_won"] - merged_stats["team_drafts_tied"])
        if team_games > 0:
            merged_stats["team_draft_win_percentage"] = (merged_stats["team_drafts_won"] / team_games) * 100
        else:
            merged_stats["team_draft_win_percentage"] = 0
        
        return merged_stats
    else:
        # Just return current stats if not the legacy guild
        return current_stats

# Modified version of get_head_to_head_stats to incorporate legacy data
async def get_head_to_head_stats_with_legacy(user1_id, user2_id, user1_display_name=None, user2_display_name=None, guild_id=None):
    """
    Get head-to-head statistics, combining current and legacy data.
    
    Args:
        user1_id: First player's Discord user ID
        user2_id: Second player's Discord user ID
        user1_display_name: Display name of the first user (optional)
        user2_display_name: Display name of the second user (optional)
        guild_id: Guild ID to filter stats by (optional)
        
    Returns:
        Dictionary with combined head-to-head stats
    """
    # Get current stats from the database
    current_stats = await get_head_to_head_stats(user1_id, user2_id, user1_display_name, user2_display_name, guild_id)
    
    # Only include legacy data if we're looking at the specific guild
    if guild_id == LEGACY_GUILD_ID:
        # Get legacy stats for each time frame
        legacy_lifetime = get_legacy_head_to_head_stats(user1_id, user2_id)
        legacy_monthly = get_legacy_head_to_head_stats(user1_id, user2_id, 'month')
        legacy_weekly = get_legacy_head_to_head_stats(user1_id, user2_id, 'week')
        
        # Merge lifetime match stats
        current_lifetime = current_stats.get("lifetime", {})
        legacy_lifetime_matches = legacy_lifetime.get("match_stats", {})
        
        merged_lifetime = {
            "matches_played": current_lifetime.get("matches_played", 0) + legacy_lifetime_matches.get("matches_played", 0),
            "user1_wins": current_lifetime.get("user1_wins", 0) + legacy_lifetime_matches.get("user1_wins", 0),
            "user2_wins": current_lifetime.get("user2_wins", 0) + legacy_lifetime_matches.get("user2_wins", 0)
        }
        
        # Recalculate percentages
        if merged_lifetime["matches_played"] > 0:
            merged_lifetime["user1_win_percentage"] = (merged_lifetime["user1_wins"] / merged_lifetime["matches_played"]) * 100
            merged_lifetime["user2_win_percentage"] = (merged_lifetime["user2_wins"] / merged_lifetime["matches_played"]) * 100
        else:
            merged_lifetime["user1_win_percentage"] = 0
            merged_lifetime["user2_win_percentage"] = 0
        
        # Merge monthly match stats
        current_monthly = current_stats.get("monthly", {})
        legacy_monthly_matches = legacy_monthly.get("match_stats", {})
        
        merged_monthly = {
            "matches_played": current_monthly.get("matches_played", 0) + legacy_monthly_matches.get("matches_played", 0),
            "user1_wins": current_monthly.get("user1_wins", 0) + legacy_monthly_matches.get("user1_wins", 0),
            "user2_wins": current_monthly.get("user2_wins", 0) + legacy_monthly_matches.get("user2_wins", 0)
        }
        
        # Recalculate percentages
        if merged_monthly["matches_played"] > 0:
            merged_monthly["user1_win_percentage"] = (merged_monthly["user1_wins"] / merged_monthly["matches_played"]) * 100
            merged_monthly["user2_win_percentage"] = (merged_monthly["user2_wins"] / merged_monthly["matches_played"]) * 100
        else:
            merged_monthly["user1_win_percentage"] = 0
            merged_monthly["user2_win_percentage"] = 0
        
        # Merge weekly match stats
        current_weekly = current_stats.get("weekly", {})
        legacy_weekly_matches = legacy_weekly.get("match_stats", {})
        
        merged_weekly = {
            "matches_played": current_weekly.get("matches_played", 0) + legacy_weekly_matches.get("matches_played", 0),
            "user1_wins": current_weekly.get("user1_wins", 0) + legacy_weekly_matches.get("user1_wins", 0),
            "user2_wins": current_weekly.get("user2_wins", 0) + legacy_weekly_matches.get("user2_wins", 0)
        }
        
        # Recalculate percentages
        if merged_weekly["matches_played"] > 0:
            merged_weekly["user1_win_percentage"] = (merged_weekly["user1_wins"] / merged_weekly["matches_played"]) * 100
            merged_weekly["user2_win_percentage"] = (merged_weekly["user2_wins"] / merged_weekly["matches_played"]) * 100
        else:
            merged_weekly["user1_win_percentage"] = 0
            merged_weekly["user2_win_percentage"] = 0
        
        # Merge opposing team stats
        current_opposing_lifetime = current_stats.get("opposing_lifetime", {})
        legacy_opposing_lifetime = legacy_lifetime.get("opposing_stats", {})
        
        merged_opposing_lifetime = {
            "wins": current_opposing_lifetime.get("wins", 0) + legacy_opposing_lifetime.get("wins", 0),
            "losses": current_opposing_lifetime.get("losses", 0) + legacy_opposing_lifetime.get("losses", 0),
            "draws": current_opposing_lifetime.get("draws", 0) + legacy_opposing_lifetime.get("draws", 0)
        }
        
        # Recalculate win percentage
        wins_plus_losses = merged_opposing_lifetime["wins"] + merged_opposing_lifetime["losses"]
        if wins_plus_losses > 0:
            merged_opposing_lifetime["win_percentage"] = (merged_opposing_lifetime["wins"] / wins_plus_losses) * 100
        else:
            merged_opposing_lifetime["win_percentage"] = 0
        
        # Similar merge for monthly and weekly opposing stats
        current_opposing_monthly = current_stats.get("opposing_monthly", {})
        legacy_opposing_monthly = legacy_monthly.get("opposing_stats", {})
        
        merged_opposing_monthly = {
            "wins": current_opposing_monthly.get("wins", 0) + legacy_opposing_monthly.get("wins", 0),
            "losses": current_opposing_monthly.get("losses", 0) + legacy_opposing_monthly.get("losses", 0),
            "draws": current_opposing_monthly.get("draws", 0) + legacy_opposing_monthly.get("draws", 0)
        }
        
        wins_plus_losses = merged_opposing_monthly["wins"] + merged_opposing_monthly["losses"]
        if wins_plus_losses > 0:
            merged_opposing_monthly["win_percentage"] = (merged_opposing_monthly["wins"] / wins_plus_losses) * 100
        else:
            merged_opposing_monthly["win_percentage"] = 0
        
        current_opposing_weekly = current_stats.get("opposing_weekly", {})
        legacy_opposing_weekly = legacy_weekly.get("opposing_stats", {})
        
        merged_opposing_weekly = {
            "wins": current_opposing_weekly.get("wins", 0) + legacy_opposing_weekly.get("wins", 0),
            "losses": current_opposing_weekly.get("losses", 0) + legacy_opposing_weekly.get("losses", 0),
            "draws": current_opposing_weekly.get("draws", 0) + legacy_opposing_weekly.get("draws", 0)
        }
        
        wins_plus_losses = merged_opposing_weekly["wins"] + merged_opposing_weekly["losses"]
        if wins_plus_losses > 0:
            merged_opposing_weekly["win_percentage"] = (merged_opposing_weekly["wins"] / wins_plus_losses) * 100
        else:
            merged_opposing_weekly["win_percentage"] = 0
        
        # Merge teammate stats
        current_teammate_lifetime = current_stats.get("teammate_lifetime", {})
        legacy_teammate_lifetime = legacy_lifetime.get("teammate_stats", {})
        
        merged_teammate_lifetime = {
            "wins": current_teammate_lifetime.get("wins", 0) + legacy_teammate_lifetime.get("wins", 0),
            "losses": current_teammate_lifetime.get("losses", 0) + legacy_teammate_lifetime.get("losses", 0),
            "draws": current_teammate_lifetime.get("draws", 0) + legacy_teammate_lifetime.get("draws", 0)
        }
        
        wins_plus_losses = merged_teammate_lifetime["wins"] + merged_teammate_lifetime["losses"]
        if wins_plus_losses > 0:
            merged_teammate_lifetime["win_percentage"] = (merged_teammate_lifetime["wins"] / wins_plus_losses) * 100
        else:
            merged_teammate_lifetime["win_percentage"] = 0
        
        # Similar merge for monthly and weekly teammate stats
        current_teammate_monthly = current_stats.get("teammate_monthly", {})
        legacy_teammate_monthly = legacy_monthly.get("teammate_stats", {})
        
        merged_teammate_monthly = {
            "wins": current_teammate_monthly.get("wins", 0) + legacy_teammate_monthly.get("wins", 0),
            "losses": current_teammate_monthly.get("losses", 0) + legacy_teammate_monthly.get("losses", 0),
            "draws": current_teammate_monthly.get("draws", 0) + legacy_teammate_monthly.get("draws", 0)
        }
        
        wins_plus_losses = merged_teammate_monthly["wins"] + merged_teammate_monthly["losses"]
        if wins_plus_losses > 0:
            merged_teammate_monthly["win_percentage"] = (merged_teammate_monthly["wins"] / wins_plus_losses) * 100
        else:
            merged_teammate_monthly["win_percentage"] = 0
        
        current_teammate_weekly = current_stats.get("teammate_weekly", {})
        legacy_teammate_weekly = legacy_weekly.get("teammate_stats", {})
        
        merged_teammate_weekly = {
            "wins": current_teammate_weekly.get("wins", 0) + legacy_teammate_weekly.get("wins", 0),
            "losses": current_teammate_weekly.get("losses", 0) + legacy_teammate_weekly.get("losses", 0),
            "draws": current_teammate_weekly.get("draws", 0) + legacy_teammate_weekly.get("draws", 0)
        }
        
        wins_plus_losses = merged_teammate_weekly["wins"] + merged_teammate_weekly["losses"]
        if wins_plus_losses > 0:
            merged_teammate_weekly["win_percentage"] = (merged_teammate_weekly["wins"] / wins_plus_losses) * 100
        else:
            merged_teammate_weekly["win_percentage"] = 0
        
        # Construct final merged stats
        merged_stats = {
            "user1_id": current_stats.get("user1_id", user1_id),
            "user2_id": current_stats.get("user2_id", user2_id),
            "user1_display_name": current_stats.get("user1_display_name", user1_display_name),
            "user2_display_name": current_stats.get("user2_display_name", user2_display_name),
            "weekly": merged_weekly,
            "monthly": merged_monthly,
            "lifetime": merged_lifetime,
            "opposing_weekly": merged_opposing_weekly,
            "opposing_monthly": merged_opposing_monthly,
            "opposing_lifetime": merged_opposing_lifetime,
            "teammate_weekly": merged_teammate_weekly,
            "teammate_monthly": merged_teammate_monthly,
            "teammate_lifetime": merged_teammate_lifetime
        }
        
        return merged_stats
    else:
        # Just return current stats if not the legacy guild
        return current_stats