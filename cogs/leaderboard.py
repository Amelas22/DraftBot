import discord
from discord.ext import commands
from discord.ui import View, Button
from datetime import datetime, timedelta
from sqlalchemy import text, select, bindparam, inspect
from database.db_session import db_session
from models.leaderboard_message import LeaderboardMessage
from loguru import logger
import json

class TimeframeView(View):
    def __init__(self, bot, guild_id, category, current_timeframe="lifetime"):
        super().__init__(timeout=600)  # 10 minute timeout
        self.bot = bot
        self.guild_id = guild_id
        self.category = category
        self.current_timeframe = current_timeframe
        
        # Add timeframe buttons
        timeframes = [
            ("14d", "14 Days"),
            ("30d", "30 Days"),
            ("90d", "90 Days"),
            ("lifetime", "Lifetime")
        ]
        
        for value, label in timeframes:
            # Make the current timeframe button appear selected
            button = Button(
                label=label,
                style=discord.ButtonStyle.primary if value == current_timeframe else discord.ButtonStyle.secondary,
                custom_id=f"timeframe_{value}_{category}"
            )
            button.callback = self.timeframe_callback
            self.add_item(button)
    
    async def timeframe_callback(self, interaction):
        # Extract timeframe from the button's custom_id
        custom_id = interaction.data["custom_id"]
        timeframe = custom_id.split("_")[1]
        category = self.category
        
        # Update the leaderboard with the new timeframe
        embed = await create_leaderboard_embed(self.guild_id, category, timeframe=timeframe)
        
        # Create a new view with the updated timeframe
        view = TimeframeView(self.bot, self.guild_id, category, current_timeframe=timeframe)
        
        # Update the message with the new embed and view
        await interaction.response.edit_message(embed=embed, view=view)
        
        # Update the database to reflect the new timeframe
        async with db_session() as session:
            stmt = select(LeaderboardMessage).where(LeaderboardMessage.guild_id == self.guild_id)
            result = await session.execute(stmt)
            leaderboard_message = result.scalar_one_or_none()
            
            if leaderboard_message:
                # Update the timeframe for this category
                setattr(leaderboard_message, f"{category}_timeframe", timeframe)
                await session.commit()
                logger.info(f"Updated {category} timeframe to {timeframe} for guild {self.guild_id}")

class LeaderboardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        logger.info("Leaderboard commands registered")
        
        # Register the button handler
        bot.add_listener(self.on_interaction, "on_interaction")
    
    async def on_interaction(self, interaction):
        """Handle button interactions"""
        if interaction.type == discord.InteractionType.component:
            custom_id = interaction.data.get("custom_id", "")
            if custom_id.startswith("timeframe_"):
                # This will be handled by the View callback
                pass
    
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
        
        # # Get the guild ID
        guild_id = str(ctx.guild.id)
        
        logger.info(f"Generating all leaderboards for guild {guild_id}")
        
        # Get the existing leaderboard message record
        async with db_session() as session:
            stmt = select(LeaderboardMessage).where(LeaderboardMessage.guild_id == guild_id)
            result = await session.execute(stmt)
            leaderboard_message = result.scalar_one_or_none()
            
            # Check if LeaderboardMessage has the column attributes we need
            if leaderboard_message:
                # Ensure the LeaderboardMessage object has our required columns
                missing_columns = await self.check_missing_columns(session, leaderboard_message)
                if missing_columns:
                    logger.warning(f"Some columns are missing from leaderboard_messages table: {missing_columns}")
                    # Show warning but continue anyway
            
            # Get timeframes for each category from database or defaults
            timeframes = {}
            for category in categories:
                if category == "hot_streak":
                    timeframes[category] = "7d"  # Hot streak is always 7 days
                else:
                    # Get stored timeframe or default to "lifetime"
                    if leaderboard_message and hasattr(leaderboard_message, f"{category}_timeframe"):
                        timeframes[category] = getattr(leaderboard_message, f"{category}_timeframe") or "lifetime"
                    else:
                        timeframes[category] = "lifetime"
        
        # Create or update leaderboard message for each category
        try:
            # Get or create leaderboard message record
            if not leaderboard_message:
                leaderboard_message = LeaderboardMessage(
                    guild_id=guild_id,
                    channel_id=str(ctx.channel.id),
                    message_id="placeholder",  # Will update this later
                    last_updated=datetime.now()
                )
                async with db_session() as session:
                    session.add(leaderboard_message)
                    await session.commit()
                    
                # Refresh to get the ID
                stmt = select(LeaderboardMessage).where(LeaderboardMessage.guild_id == guild_id)
                result = await session.execute(stmt)
                leaderboard_message = result.scalar_one_or_none()
            
            # Update channel ID if needed (in case the command is used in a different channel)
            if leaderboard_message.channel_id != str(ctx.channel.id):
                async with db_session() as session:
                    leaderboard_message.channel_id = str(ctx.channel.id)
                    await session.commit()

            # Try to get the channel
            channel = ctx.guild.get_channel(int(leaderboard_message.channel_id))
            if not channel:
                raise ValueError(f"Channel {leaderboard_message.channel_id} not found")
            
            # Process each category
            for category in categories:
                logger.info(f"Processing {category} leaderboard")
                
                # Create the embed
                embed = await create_leaderboard_embed(guild_id, category, timeframe=timeframes[category])
                
                # Create view for categories except hot_streak
                view = None
                if category != "hot_streak":
                    view = TimeframeView(self.bot, guild_id, category, current_timeframe=timeframes[category])
                
                # Get the message ID field name
                msg_id_field = f"{category}_view_message_id" if category != "hot_streak" else "message_id"
                
                # Try to update existing message
                message_updated = False
                if hasattr(leaderboard_message, msg_id_field) and getattr(leaderboard_message, msg_id_field):
                    try:
                        message_id = getattr(leaderboard_message, msg_id_field)
                        existing_msg = await channel.fetch_message(int(message_id))
                        if category != "hot_streak":
                            await existing_msg.edit(embed=embed, view=view)
                        else:
                            await existing_msg.edit(embed=embed)
                        message_updated = True
                        logger.info(f"Updated existing {category} message {message_id}")
                    except discord.NotFound:
                        logger.warning(f"Message {message_id} for {category} not found, will create new one")
                    except Exception as e:
                        logger.error(f"Error updating {category} message: {e}")
                
                # Send new message if needed
                if not message_updated:
                    try:
                        if category != "hot_streak":
                            new_msg = await channel.send(embed=embed, view=view)
                            setattr(leaderboard_message, f"{category}_view_message_id", str(new_msg.id))
                        else:
                            new_msg = await channel.send(embed=embed)
                            leaderboard_message.message_id = str(new_msg.id)
                        
                        async with db_session() as session:
                            session.add(leaderboard_message)
                            await session.commit()
                        logger.info(f"Created new {category} message")
                    except Exception as e:
                        logger.error(f"Error creating new {category} message: {e}")
            
            # Update last_updated timestamp
            async with db_session() as session:
                leaderboard_message.last_updated = datetime.now()
                await session.commit()
            
            # Complete the interaction
            await ctx.respond("✅")
            
        except Exception as e:
            logger.error(f"Error processing leaderboards: {e}")
            await ctx.respond(f"Error creating leaderboards: {str(e)}")
    
    async def check_missing_columns(self, session, leaderboard_message):
        """Check if any required columns are missing from the leaderboard_messages table"""
        missing_columns = []
        
        # Required columns for the timeframe functionality
        required_columns = [
            "draft_record_view_message_id",
            "match_win_view_message_id",
            "drafts_played_view_message_id", 
            "time_vault_and_key_view_message_id",
            "draft_record_timeframe",
            "match_win_timeframe",
            "drafts_played_timeframe",
            "time_vault_and_key_timeframe"
        ]
        
        # Check for each required column
        for column in required_columns:
            if not hasattr(leaderboard_message, column):
                missing_columns.append(column)
        
        return missing_columns

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

        # Adjust minimum requirements based on timeframe
        min_drafts = 25
        min_matches = 50
        min_partnership_drafts = 8
        
        if timeframe == "14d":
            min_drafts = 5
            min_matches = 12
            min_partnership_drafts = 3
        elif timeframe == "30d":
            min_drafts = 10
            min_matches = 25
            min_partnership_drafts = 3
        elif timeframe == "90d":
            min_drafts = 20
            min_matches = 45
            min_partnership_drafts = 6
        
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
        else:
            # Default to drafts_played if category not recognized
            sorted_players = sorted(players_list, key=lambda p: p["drafts_played"], reverse=True)
        
        # Limit to requested number
        return sorted_players[:limit]
    
async def create_leaderboard_embed(guild_id, category="draft_record", limit=20, timeframe="lifetime"):
    """Create an embed with leaderboard data"""
    # Get the leaderboard data
    leaderboard_data = await get_leaderboard_data(guild_id, category, limit, timeframe)
    
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
    
    # Format timeframe for display
    timeframe_display = {
        "7d": "Last 7 Days",
        "14d": "Last 14 Days",
        "30d": "Last 30 Days",
        "90d": "Last 90 Days",
        "lifetime": "Lifetime"
    }.get(timeframe, "Lifetime")

    min_drafts = 25
    min_matches = 50
    min_partnership_drafts = 8
    
    if timeframe == "14d":
        min_drafts = 5
        min_matches = 12
        min_partnership_drafts = 3
    elif timeframe == "30d":
        min_drafts = 10
        min_matches = 25
        min_partnership_drafts = 3
    elif timeframe == "90d":
        min_drafts = 20
        min_matches = 45
        min_partnership_drafts = 6

    # Define category titles and descriptions
    categories = {
        "draft_record": {
            "title": f"Draft Record Leaderboard ({timeframe_display})",
            "description": f"Players with the highest team draft win percentage (min {min_drafts} drafts, 50%+ win rate)",
            "formatter": lambda p, rank: f"{get_medal(rank)}**{p['display_name']}**: {p['team_drafts_won']}-{p['team_drafts_lost']}-{p['team_drafts_tied']} ({p['team_draft_win_percentage']:.1f}%)"
        },
        "match_win": {
            "title": f"Match Win Leaderboard ({timeframe_display})",
            "description": f"Players with the highest individual match win percentage (min {min_matches} matches, 50%+ win rate)",
            "formatter": lambda p, rank: f"{get_medal(rank)}{p['display_name']}: {p['matches_won']}/{p['completed_matches']} ({p['match_win_percentage']:.1f}%)"
        },
        "drafts_played": {
            "title": f"Drafts Played Leaderboard ({timeframe_display})",
            "description": f"Players who have participated in the most drafts",
            "formatter": lambda p, rank: f"{get_medal(rank)}{p['display_name']}: {p['drafts_played']} drafts"
        },
        "time_vault_and_key": {
            "title": f"Vault / Key Leaderboard ({timeframe_display})",
            "description": f"Highest Draft Win Rate when paired as teammates (min {min_partnership_drafts} drafts together, 50%+ win rate)",
            "formatter": lambda p, rank: f"{get_medal(rank)}{p['player_name']} & {p['teammate_name']}: {p['drafts_won']}-{p['drafts_lost']}-{p['drafts_tied']} ({p['win_percentage']:.1f}%)"
        },
        "hot_streak": {
            "title": "Hot Streak Leaderboard (Last 7 Days)",
            "description": "Players with the best match win % in the last 7 days (min 9 matches, 50%+ win rate)",
            "formatter": lambda p, rank: f"{get_medal(rank)}{p['display_name']}: {p['matches_won']}/{p['completed_matches']} ({p['match_win_percentage']:.1f}%)"
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
    
    embed.set_footer(text="Choose a filter to refresh stats")
    
    return embed

def setup(bot):
    bot.add_cog(LeaderboardCog(bot))