import discord
from discord.ext import commands
from datetime import datetime, timedelta
from sqlalchemy import text, select
from database.db_session import db_session
from models.leaderboard_message import LeaderboardMessage
from loguru import logger
import json

class LeaderboardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        logger.info("Leaderboard commands registered")

    @discord.slash_command(name="leaderboard", description="Show all player leaderboards for drafts")
    async def leaderboard(self, ctx):
        """Display all leaderboards of player statistics"""
        await ctx.defer()
        
        # All categories to display
        categories = [
            "draft_record",
            "match_win",
            "drafts_played", 
            "time_vault_and_key",
            "hot_streak"
        ]
        
        # Get the guild ID
        guild_id = str(ctx.guild.id)
        
        logger.info(f"Generating all leaderboards for guild {guild_id}")
        
        # Create embeds for all categories
        embeds = []
        for category in categories:
            logger.info(f"Creating embed for category: {category}")
            embed = await create_leaderboard_embed(guild_id, category)
            embeds.append(embed)
        
        # Check if we have an existing leaderboard message
        async with db_session() as session:
            stmt = select(LeaderboardMessage).where(LeaderboardMessage.guild_id == guild_id)
            result = await session.execute(stmt)
            leaderboard_message = result.scalar_one_or_none()
            
            if leaderboard_message:
                # Try to get the existing message and update it
                try:
                    channel = ctx.guild.get_channel(int(leaderboard_message.channel_id))
                    if channel:
                        try:
                            message = await channel.fetch_message(int(leaderboard_message.message_id))
                            await message.edit(embeds=embeds)
                            await ctx.respond("✅")
                            return
                        except discord.NotFound:
                            # Message not found, will create a new one
                            logger.info("Leaderboard message not found, creating a new one")
                    else:
                        logger.info("Leaderboard channel not found, creating a new message")
                except Exception as e:
                    logger.error(f"Error updating leaderboard: {e}")
            
            # Create a new message directly in the channel
            try:
                channel = ctx.channel
                new_message = await channel.send(embeds=embeds)
                
                # Create or update leaderboard message record
                if leaderboard_message:
                    leaderboard_message.channel_id = str(channel.id)
                    leaderboard_message.message_id = str(new_message.id)
                    leaderboard_message.last_updated = datetime.now()
                else:
                    leaderboard_message = LeaderboardMessage(
                        guild_id=guild_id,
                        channel_id=str(channel.id),
                        message_id=str(new_message.id),
                        last_updated=datetime.now()
                    )
                    session.add(leaderboard_message)
                
                await session.commit()
                
                # Complete the interaction with a simple response
                await ctx.respond("✅")
                
            except Exception as e:
                logger.error(f"Error creating leaderboard: {e}")
                await ctx.respond("Error creating leaderboards.")

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

async def get_leaderboard_data(guild_id, category="draft_record", limit=20):
    """Get leaderboard data for all players in a guild"""

    now = datetime.now()
    week_ago = now - timedelta(days=7)
    
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
        drafts_query = text(drafts_query_text)
        drafts_result = await session.execute(drafts_query, {"guild_id": guild_id})
        all_drafts = drafts_result.fetchall()
        logger.info(f"Found {len(all_drafts)} completed drafts in guild {guild_id}")
        
        # Get all match results in one query
        match_results_query_text = """
            SELECT session_id, player1_id, player2_id, winner_id
            FROM match_results
            WHERE session_id IN (
                SELECT session_id FROM draft_sessions 
                WHERE session_type IN ('random', 'staked')
                AND victory_message_id_results_channel IS NOT NULL
                AND guild_id = :guild_id
            )
        """
        match_results_query = text(match_results_query_text)
        match_results_result = await session.execute(match_results_query, {"guild_id": guild_id})
        all_matches = match_results_result.fetchall()
        
        # Organize match results by session
        match_results_by_session = {}
        for session_id, p1_id, p2_id, winner_id in all_matches:
            if session_id not in match_results_by_session:
                match_results_by_session[session_id] = []
            match_results_by_session[session_id].append((p1_id, p2_id, winner_id))
        
        # Process all drafts and build player data
        player_counts = {}  # Track draft counts for all players
        
        for draft_id, session_id, team_a_json, team_b_json, sign_ups_json, teams_start_time in all_drafts:
            try:
                # Parse sign_ups
                sign_ups = json.loads(sign_ups_json) if isinstance(sign_ups_json, str) else sign_ups_json or {}
                
                # Parse teams for win/loss calculations
                team_a = json.loads(team_a_json) if isinstance(team_a_json, str) else team_a_json or []
                team_b = json.loads(team_b_json) if isinstance(team_b_json, str) else team_b_json or []
                
                # Check if this is a recent draft
                is_recent = False
                if teams_start_time:
                    # Ensure teams_start_time is a datetime object
                    if isinstance(teams_start_time, str):
                        try:
                            teams_start_time = datetime.fromisoformat(teams_start_time.replace('Z', '+00:00'))
                        except (ValueError, TypeError):
                            logger.error(f"Error converting teams_start_time for draft {draft_id}")
                    
                    # Compare with week_ago 
                    if isinstance(teams_start_time, datetime):
                        is_recent = teams_start_time >= week_ago
                        if is_recent:
                            logger.debug(f"Draft {draft_id} is recent: {teams_start_time}")
                
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
                            "matches_lost": 0,  # Directly track losses
                            "match_win_percentage": 0,
                            "team_drafts_played": 0,
                            "team_drafts_won": 0,
                            "team_drafts_tied": 0,
                            "team_drafts_lost": 0,
                            "team_draft_win_percentage": 0,
                            "last_30_days_drafts": 0,
                            "last_30_days_completed_matches": 0,  # Only count matches with a result
                            "last_30_days_matches_won": 0,
                            "last_30_days_matches_lost": 0,  # Directly track recent losses
                            "last_30_days_team_drafts_played": 0,
                            "last_30_days_team_drafts_won": 0,
                            "last_30_days_team_drafts_lost": 0,
                            "last_30_days_team_drafts_tied": 0,
                            "last_30_days_team_draft_win_percentage": 0,
                            "teammate_win_rates": {}
                        }
                    else:
                        # Update display name if needed
                        players_data[player_id]["display_name"] = display_name
                    
                    # Update drafts played count - this is the core of what you described
                    players_data[player_id]["drafts_played"] += 1
                    if is_recent:
                        players_data[player_id]["last_30_days_drafts"] += 1
                    
                    # Update individual matches played/won - FIXED MATCH COUNTING
                    for p1_id, p2_id, winner_id in session_matches:
                        if p1_id == player_id or p2_id == player_id:
                            # Only count matches that have a determined winner
                            if winner_id is not None:
                                players_data[player_id]["completed_matches"] += 1
                                if is_recent:
                                    players_data[player_id]["last_30_days_completed_matches"] += 1
                                
                                if winner_id == player_id:
                                    players_data[player_id]["matches_won"] += 1
                                    if is_recent:
                                        players_data[player_id]["last_30_days_matches_won"] += 1
                                else:
                                    # Explicitly count losses when the winner is not the player
                                    players_data[player_id]["matches_lost"] += 1
                                    if is_recent:
                                        players_data[player_id]["last_30_days_matches_lost"] += 1
                    
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
                        if is_recent:
                            players_data[player_id]["last_30_days_team_drafts_played"] += 1
                        
                        # Determine draft result based on winner_team
                        if winner_team == "Draw":
                            players_data[player_id]["team_drafts_tied"] += 1
                            if is_recent:
                                players_data[player_id]["last_30_days_team_drafts_tied"] += 1
                        elif winner_team == player_team:
                            players_data[player_id]["team_drafts_won"] += 1
                            if is_recent:
                                players_data[player_id]["last_30_days_team_drafts_won"] += 1
                        else:
                            players_data[player_id]["team_drafts_lost"] += 1
                            if is_recent:
                                players_data[player_id]["last_30_days_team_drafts_lost"] += 1
                        
                        # Update teammate stats - same pattern as your existing code
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
        
        for player_id, player_data in players_data.items():
            # FIXED: Count matches with completed matches
            if player_data["completed_matches"] > 0:
                player_data["match_win_percentage"] = (player_data["matches_won"] / player_data["completed_matches"]) * 100
            
            # Count team drafts
            team_draft_counted_drafts = player_data["team_drafts_won"] + player_data["team_drafts_lost"]
            if team_draft_counted_drafts > 0:
                player_data["team_draft_win_percentage"] = (player_data["team_drafts_won"] / team_draft_counted_drafts) * 100
            
            # Count recent stats
            last_30_days_counted_drafts = player_data["last_30_days_team_drafts_won"] + player_data["last_30_days_team_drafts_lost"]
            if last_30_days_counted_drafts > 0:
                player_data["last_30_days_team_draft_win_percentage"] = (player_data["last_30_days_team_drafts_won"] / last_30_days_counted_drafts) * 100
            
            # FIXED: Count recent matches with completed matches
            if player_data["last_30_days_completed_matches"] > 0:
                player_data["last_30_days_match_win_percentage"] = (player_data["last_30_days_matches_won"] / player_data["last_30_days_completed_matches"]) * 100
            

        # Log some statistics to understand the difference
        players_with_5_plus_drafts = sum(1 for p in players_data.values() if p["drafts_played"] >= 5)
        players_with_5_plus_team_drafts = sum(1 for p in players_data.values() if p["team_drafts_played"] >= 5)
        logger.info(f"Found {players_with_5_plus_drafts} players with 5+ drafts played")
        logger.info(f"But only {players_with_5_plus_team_drafts} players with 5+ team drafts played")

        # Convert to list for sorting
        players_list = list(players_data.values())

        # CHANGE THE FILTERING FOR CATEGORIES TO USE drafts_played INSTEAD:
        if category == "draft_record":
            filtered_players = [p for p in players_list if p["drafts_played"] >= 5 and p["team_draft_win_percentage"] >= 50]
            logger.info(f"Found {len(filtered_players)} players with at least 5 drafts for draft_record")
            # Sort by team draft win percentage (descending)
            sorted_players = sorted(filtered_players, key=lambda p: p["team_draft_win_percentage"], reverse=True)
        
        elif category == "match_win":
            # FIXED: Change to filter using completed_matches
            filtered_players = [p for p in players_list if p["completed_matches"] >= 15 and p["match_win_percentage"] >= 50]
            logger.info(f"Found {len(filtered_players)} players with at least 5 completed matches for match_win")
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
                
                # For debugging - print a sample of relationships
                if len(player_data["teammate_win_rates"]) > 0 and len(best_partnerships) == 0:
                    sample_teammate = next(iter(player_data["teammate_win_rates"].values()))
                    logger.info(f"Sample teammate relationship: {player_data['display_name']} + {sample_teammate['teammate_name']}: {sample_teammate['drafts_played']} drafts, {sample_teammate['drafts_won']} wins")
                
                for teammate_id, teammate_data in player_data["teammate_win_rates"].items():
                    # Create a unique key for the pair (sorted to avoid duplicate direction)
                    pair_key = tuple(sorted([player_id, teammate_id]))
                    if pair_key in seen_pairs:
                        continue  # Skip already processed pair
                    seen_pairs.add(pair_key)

                    counted_drafts = teammate_data["drafts_won"] + teammate_data["drafts_lost"]
                    if counted_drafts >= 3:
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
            logger.info(f"Found {len(best_partnerships)} partnerships with at least 3 drafts together")
            
            # Sort partnerships by win percentage
            sorted_players = sorted(best_partnerships, key=lambda p: p["win_percentage"], reverse=True)

        
        elif category == "hot_streak":
                filtered_players = [p for p in players_list if p["last_30_days_completed_matches"] >= 9 and p["last_30_days_match_win_percentage"] > 50]
                logger.info(f"Found {len(filtered_players)} players with at least 9 completed matches in last 7 days for hot_streak")
                # Sort by 7-day match win percentage
                sorted_players = sorted(filtered_players, key=lambda p: p["last_30_days_match_win_percentage"], reverse=True)

        else:
            # Default to drafts_played if category not recognized
            sorted_players = sorted(players_list, key=lambda p: p["drafts_played"], reverse=True)
        
        # Limit to requested number
        return sorted_players[:limit]
    
async def create_leaderboard_embed(guild_id, category="draft_record", limit=20):
    """Create an embed with leaderboard data"""
    # Get the leaderboard data
    leaderboard_data = await get_leaderboard_data(guild_id, category, limit)
    
    # Helper function to add medals
    def get_medal(rank):
        if rank == 1:
            return "1. 🥇 "
        elif rank == 2:
            return "2. 🥈 "
        elif rank == 3:
            return "3. 🥉 "
        else:
            return f"{rank}. "
    
    # Define category titles and descriptions
    categories = {
        "draft_record": {
            "title": "Draft Record Leaderboard",
            "description": "Players with the highest team draft win percentage (min 5 drafts)",
            "formatter": lambda p, rank: f"{get_medal(rank)}**{p['display_name']}**: {p['team_drafts_won']}-{p['team_drafts_lost']}-{p['team_drafts_tied']} ({p['team_draft_win_percentage']:.1f}%)"
        },
        "match_win": {
            "title": "Match Win Leaderboard",
            "description": "Players with the highest individual match win percentage (min 5 matches)",
            "formatter": lambda p, rank: f"{get_medal(rank)}{p['display_name']}: {p['matches_won']}/{p['completed_matches']} ({p['match_win_percentage']:.1f}%)"
        },
        "drafts_played": {
            "title": "Drafts Played Leaderboard",
            "description": "Players who have participated in the most drafts",
            "formatter": lambda p, rank: f"{get_medal(rank)}{p['display_name']}: {p['drafts_played']} drafts"
        },
        "time_vault_and_key": {
            "title": "Vault / Key Leaderboard",
            "description": "Highest Draft Win Rate when paired as teammates (min 3 drafts together)",
            "formatter": lambda p, rank: f"{get_medal(rank)}{p['player_name']} & {p['teammate_name']}: {p['drafts_won']}-{p['drafts_lost']}-{p['drafts_tied']} ({p['win_percentage']:.1f}%)"
        },
        "hot_streak": {
            "title": "Hot Streak Leaderboard (Last 7 Days)",
            "description": "Players with the best match win % in the last 7 days (min 3 matches)",
            "formatter": lambda p, rank: f"{get_medal(rank)}{p['display_name']}: {p['last_30_days_matches_won']}/{p['last_30_days_completed_matches']} ({p['last_30_days_match_win_percentage']:.1f}%)"
        }
    }
    
    # Create the embed
    embed = discord.Embed(
        title=categories[category]["title"],
        description=categories[category]["description"],
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    
    # Add leaderboard data
    if not leaderboard_data:
        embed.add_field(name="No Data", value="No players found matching the criteria")
    else:
        # Format leaderboard entries
        entries = []
        for i, player in enumerate(leaderboard_data, 1):
            entry = categories[category]["formatter"](player, i)
            entries.append(entry)
        
        # Add all entries in a single field
        embed.add_field(name="Rankings", value="\n".join(entries), inline=False)
    
    embed.set_footer(text="")
    
    return embed

def setup(bot):
    bot.add_cog(LeaderboardCog(bot))