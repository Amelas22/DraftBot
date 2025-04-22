import asyncio
import sys
from sqlalchemy import select, and_
from loguru import logger
from datetime import datetime

# Import the database session and models
from database.db_session import db_session
from models.draft_session import DraftSession
from models.match import MatchResult
from models.stake import StakeInfo

def map_to_standard_stake(stake):
    """Map an actual stake amount to one of the standard categories"""
    if stake == 10:
        return "10"
    elif stake == 20:
        return "20"
    elif stake == 50:
        return "50"
    elif stake == 100:
        return "100"
    elif stake > 100:
        return ">100"
    else:
        # For non-standard stakes, map to the closest standard value
        if stake < 10:
            return "10"
        elif stake < 20:
            return "20"
        elif stake < 50:
            return "50"
        elif stake < 100:
            return "100"
        else:
            return ">100"

async def analyze_stake_win_rates():
    """
    Analyze win rates for staked drafts based on players' max stake values.
    Only counts matches that have a winner_id.
    Categorizes stakes into standard amounts: 10, 20, 50, 100, >100
    """
    # Track win/loss statistics by stake amount
    stake_stats = {
        "10": {"wins": 0, "losses": 0},
        "20": {"wins": 0, "losses": 0},
        "50": {"wins": 0, "losses": 0},
        "100": {"wins": 0, "losses": 0},
        ">100": {"wins": 0, "losses": 0}
    }
    
    # For tracking data quality
    guild_id = "1355718878298116096"
    total_sessions = 0
    total_matches = 0
    matches_with_missing_data = 0
    
    # Set up logger
    logger.remove()
    logger.add(sys.stdout, level="INFO")
    logger.info(f"Starting stake win rate analysis for guild {guild_id}")
    
    # Step 1: Find all staked draft sessions for the specified guild
    async with db_session() as session:
        staked_sessions_query = select(DraftSession).where(
            and_(
                DraftSession.guild_id == guild_id,
                DraftSession.session_type == "staked"
            )
        )
        staked_sessions_result = await session.execute(staked_sessions_query)
        staked_sessions = staked_sessions_result.scalars().all()
        
        total_sessions = len(staked_sessions)
        logger.info(f"Found {total_sessions} staked draft sessions in guild {guild_id}")
        
        # Process each staked session
        for draft_session in staked_sessions:
            session_id = draft_session.session_id
            logger.debug(f"Processing session {session_id}")
            
            # Get stake info for this session
            stake_info_query = select(StakeInfo).where(
                StakeInfo.session_id == session_id
            )
            stake_info_result = await session.execute(stake_info_query)
            stake_infos = stake_info_result.scalars().all()
            
            # Create a mapping of player_id to standardized max_stake
            player_stakes = {info.player_id: map_to_standard_stake(info.max_stake) for info in stake_infos}
            
            # Get match results with a winner
            match_results_query = select(MatchResult).where(
                and_(
                    MatchResult.session_id == session_id,
                    MatchResult.winner_id.isnot(None)
                )
            )
            match_results_result = await session.execute(match_results_query)
            match_results = match_results_result.scalars().all()
            
            session_matches = len(match_results)
            total_matches += session_matches
            logger.debug(f"Session {session_id}: Found {session_matches} matches with winners")
            
            # Process each match result
            for match in match_results:
                winner_id = match.winner_id
                player1_id = match.player1_id
                player2_id = match.player2_id
                
                # Skip if missing stake info for either player
                if player1_id not in player_stakes or player2_id not in player_stakes:
                    matches_with_missing_data += 1
                    logger.debug(f"Match {match.id}: Missing stake info for player1={player1_id} or player2={player2_id}")
                    continue
                
                # Get the stakes for both players (already standardized)
                player1_stake = player_stakes[player1_id]
                player2_stake = player_stakes[player2_id]
                
                # Update win/loss statistics
                if winner_id == player1_id:
                    stake_stats[player1_stake]["wins"] += 1
                    stake_stats[player2_stake]["losses"] += 1
                    logger.debug(f"Match {match.id}: Player1 ({player1_stake} tix) won against Player2 ({player2_stake} tix)")
                elif winner_id == player2_id:
                    stake_stats[player1_stake]["losses"] += 1
                    stake_stats[player2_stake]["wins"] += 1
                    logger.debug(f"Match {match.id}: Player2 ({player2_stake} tix) won against Player1 ({player1_stake} tix)")
    
    # Calculate and print results
    logger.info("\nData Summary:")
    logger.info(f"Total staked draft sessions: {total_sessions}")
    logger.info(f"Total matches with winners: {total_matches}")
    logger.info(f"Matches skipped due to missing stake info: {matches_with_missing_data}")
    logger.info(f"Matches analyzed: {total_matches - matches_with_missing_data}")
    
    logger.info("\nMax Stake Win Rate Analysis:")
    logger.info("------------------------")
    logger.info(f"{'Stake Amount':<15} {'Matches':<10} {'Wins':<8} {'Losses':<8} {'Win Rate':<10}")
    logger.info("-" * 55)
    
    # Define the order of standardized stake categories
    stake_order = ["10", "20", "50", "100", ">100"]
    
    # Display in specified order
    for stake in stake_order:
        stats = stake_stats[stake]
        wins = stats["wins"]
        losses = stats["losses"]
        total_matches = wins + losses
        win_rate = (wins / total_matches * 100) if total_matches > 0 else 0
        
        logger.info(f"{stake:<15} {total_matches:<10} {wins:<8} {losses:<8} {win_rate:.2f}%")
    
    # Overall statistics
    total_wins = sum(stats["wins"] for stats in stake_stats.values())
    total_losses = sum(stats["losses"] for stats in stake_stats.values())
    total_analyzed_matches = total_wins + total_losses
    overall_win_rate = (total_wins / total_analyzed_matches * 100) if total_analyzed_matches > 0 else 0
    
    logger.info("-" * 55)
    logger.info(f"{'Overall':<15} {total_analyzed_matches:<10} {total_wins:<8} {total_losses:<8} {overall_win_rate:.2f}%")
    
    # Write to a simple text file
    filename = f"stake_winrate_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(filename, 'w') as f:
        f.write(f"STAKE WIN RATE ANALYSIS - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Guild ID: {guild_id}\n\n")
        
        f.write("DATA SUMMARY:\n")
        f.write(f"Total staked draft sessions: {total_sessions}\n")
        f.write(f"Total matches with winners: {total_matches}\n")
        f.write(f"Matches skipped due to missing stake info: {matches_with_missing_data}\n")
        f.write(f"Matches analyzed: {total_matches - matches_with_missing_data}\n\n")
        
        f.write("STAKE AMOUNT ANALYSIS:\n")
        f.write(f"{'Stake Amount':<15} {'Matches':<10} {'Wins':<8} {'Losses':<8} {'Win Rate':<10}\n")
        f.write("-" * 55 + "\n")
        
        # Display in specified order
        for stake in stake_order:
            stats = stake_stats[stake]
            wins = stats["wins"]
            losses = stats["losses"]
            total_matches = wins + losses
            win_rate = (wins / total_matches * 100) if total_matches > 0 else 0
            
            f.write(f"{stake:<15} {total_matches:<10} {wins:<8} {losses:<8} {win_rate:.2f}%\n")
        
        f.write("-" * 55 + "\n")
        f.write(f"{'Overall':<15} {total_analyzed_matches:<10} {total_wins:<8} {total_losses:<8} {overall_win_rate:.2f}%\n")
    
    logger.info(f"\nResults saved to {filename}")
    return stake_stats

# Entry point
if __name__ == "__main__":
    async def main():
        stake_stats = await analyze_stake_win_rates()
    
    asyncio.run(main())