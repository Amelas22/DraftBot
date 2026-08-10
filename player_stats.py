import discord
from datetime import datetime
from sqlalchemy import select, func
from session import AsyncSessionLocal, PlayerStats
from models.win_streak_history import WinStreakHistory
from models.perfect_streak_history import PerfectStreakHistory
from loguru import logger
from helpers.display_names import get_member_name
from services.ledger_stats import (
    LedgerSnapshot, match_totals, draft_totals, trophy_count, team_record,
    cube_breakdown, h2h_totals)
from stats_core import get_timeframe_start_date, calculate_win_percentage, calculate_team_draft_win_percentage

# Cube-specific stats appear in the /stats embed only at this many
# completed drafts of that cube (the field title states the same number).
MIN_CUBE_DRAFTS_DISPLAY = 5

async def get_player_statistics(user_id, time_frame=None, user_display_name=None, guild_id=None,
                                snapshot=None):
    """Get player statistics for a specific user and time frame, filtered by guild_id if provided."""
    try:
        # Calculate the start date based on time frame using shared utility
        start_date = get_timeframe_start_date(time_frame)
        
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

                # Get historical streak "ended by" information for longest streaks
                longest_win_streak_ender = None
                longest_perfect_streak_ender = None

                if longest_win_streak > 0:
                    # Query for the longest completed win streak
                    win_streak_query = select(WinStreakHistory).where(
                        WinStreakHistory.player_id == user_id,
                        WinStreakHistory.guild_id == guild_id,
                        WinStreakHistory.streak_length == longest_win_streak,
                        WinStreakHistory.ended_at.isnot(None)
                    ).order_by(WinStreakHistory.ended_at.desc()).limit(1)

                    win_streak_result = await session.execute(win_streak_query)
                    longest_win_streak_record = win_streak_result.scalar_one_or_none()

                    if longest_win_streak_record and longest_win_streak_record.ended_by_player_id:
                        # Get the ender's display name
                        ender_query = select(PlayerStats).where(
                            PlayerStats.player_id == longest_win_streak_record.ended_by_player_id,
                            PlayerStats.guild_id == guild_id
                        )
                        ender_result = await session.execute(ender_query)
                        ender_stats = ender_result.scalar_one_or_none()
                        if ender_stats:
                            longest_win_streak_ender = ender_stats.display_name

                if longest_perfect_streak > 0:
                    # Query for the longest completed perfect streak
                    perfect_streak_query = select(PerfectStreakHistory).where(
                        PerfectStreakHistory.player_id == user_id,
                        PerfectStreakHistory.guild_id == guild_id,
                        PerfectStreakHistory.streak_length == longest_perfect_streak,
                        PerfectStreakHistory.ended_at.isnot(None)
                    ).order_by(PerfectStreakHistory.ended_at.desc()).limit(1)

                    perfect_streak_result = await session.execute(perfect_streak_query)
                    longest_perfect_streak_record = perfect_streak_result.scalar_one_or_none()

                    if longest_perfect_streak_record and longest_perfect_streak_record.ended_by_player_id:
                        # Get the ender's display name
                        ender_query = select(PlayerStats).where(
                            PlayerStats.player_id == longest_perfect_streak_record.ended_by_player_id,
                            PlayerStats.guild_id == guild_id
                        )
                        ender_result = await session.execute(ender_query)
                        ender_stats = ender_result.scalar_one_or_none()
                        if ender_stats:
                            longest_perfect_streak_ender = ender_stats.display_name

                # Stored names are complete -- see get_member_name's
                # docstring for why a miss just formats as "User <id>".
                if display_name == "Unknown":
                    display_name = get_member_name(None, user_id)

                # Count matches, drafts, trophies, and the team-draft record
                # from the match-result ledger (the source of truth the
                # rating system already uses) instead of display artifacts
                # like sign_ups JSON, victory-message ids, or
                # trophy_drafters name strings.
                if snapshot is None:
                    snapshot = await LedgerSnapshot.fetch(guild_id)
                records = snapshot.fold(player_id=user_id, since=start_date)
                totals = match_totals(records)
                matches_played = totals["matches_played"]
                matches_won = totals["matches_won"]
                drafts_played = draft_totals(records)
                trophies_won = trophy_count(records)
                team = team_record(records)
                team_drafts_played = team["played"]
                team_drafts_won = team["won"]
                team_drafts_tied = team["tied"]

                # Calculate match win percentage using shared utility
                matches_lost = matches_played - matches_won
                match_win_percentage = calculate_win_percentage(matches_won, matches_lost)

                # Calculate team draft win percentage using shared utility
                team_drafts_lost = team["lost"]
                team_draft_win_percentage = calculate_team_draft_win_percentage(
                    team_drafts_won, team_drafts_lost, team_drafts_tied)

                # Get stats by cube type. Wins/losses/drafts come straight
                # from the ledger fold (cube_breakdown); this dict is
                # unfiltered -- the embed applies the MIN_CUBE_DRAFTS_DISPLAY
                # threshold itself.
                cube_stats = {}
                for cube_name, cube_totals in cube_breakdown(records).items():
                    if cube_name == "Unknown":
                        # cube_breakdown buckets sessions with no cube set here;
                        # the embed only ever displayed named cubes.
                        continue
                    # Only what the embed reads: the draft count (for the
                    # min-5 display threshold) and the win percentage.
                    cube_stats[cube_name] = {
                        "drafts_played": cube_totals["drafts"],
                        "win_percentage": calculate_win_percentage(
                            cube_totals["wins"], cube_totals["losses"]),
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
                    "longest_perfect_streak": longest_perfect_streak,
                    "longest_win_streak_ender": longest_win_streak_ender,
                    "longest_perfect_streak_ender": longest_perfect_streak_ender
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

    # Show longest win streak with "ended by" if available
    longest_win = stats_lifetime['longest_win_streak']
    if stats_lifetime.get('longest_win_streak_ender'):
        lifetime_value += f"Longest Win Streak: {longest_win} (ended by {stats_lifetime['longest_win_streak_ender']})\n"
    else:
        lifetime_value += f"Longest Win Streak: {longest_win}\n"

    # Add perfect streak info
    if stats_lifetime['current_perfect_streak'] > 0:
        lifetime_value += f"🔥🔥 Current Perfect Streak: **{stats_lifetime['current_perfect_streak']}**\n"
    else:
        lifetime_value += f"Current Perfect Streak: {stats_lifetime['current_perfect_streak']}\n"

    # Show longest perfect streak with "ended by" if available
    longest_perfect = stats_lifetime['longest_perfect_streak']
    if stats_lifetime.get('longest_perfect_streak_ender'):
        lifetime_value += f"Longest Perfect Streak: {longest_perfect} (ended by {stats_lifetime['longest_perfect_streak_ender']})"
    else:
        lifetime_value += f"Longest Perfect Streak: {longest_perfect}"

    embed.add_field(
        name="Lifetime Stats",
        value=lifetime_value,
        inline=False
    )

    # 🎯 Skill Rating (injected by stats_display.get_stats_embed_for_player)
    skill = stats_lifetime.get('skill_rating')
    if skill is not None:
        value = f"{skill} (provisional)" if stats_lifetime.get('skill_provisional') else str(skill)
        value += "\n*New players start at 1500 · a 100-point gap ≈ 60% match favorite*"
        embed.add_field(name="🎯 Skill Rating", value=value, inline=False)

    # Add cube-specific stats if any are available (the field's title
    # states the same MIN_CUBE_DRAFTS_DISPLAY threshold)
    displayable_cube_stats = {
        cube_name: stats for cube_name, stats in stats_lifetime['cube_stats'].items()
        if stats['drafts_played'] >= MIN_CUBE_DRAFTS_DISPLAY
    }
    if displayable_cube_stats:
        # Convert to list and sort by drafts_played in descending order
        sorted_cube_stats = sorted(
            displayable_cube_stats.items(),
            key=lambda x: x[1]['drafts_played'],
            reverse=True
        )
        
        cube_stats_text = ""
        for cube_name, stats in sorted_cube_stats:
            cube_stats_text += f"**{cube_name}**: {stats['win_percentage']:.1f}% ({stats['drafts_played']} Drafts)\n"
        
        embed.add_field(
            name=f"Cube Win Percentage (min {MIN_CUBE_DRAFTS_DISPLAY} drafts)",
            value=cube_stats_text,
            inline=False
        )
    
    embed.set_footer(text="Stats update as match results are reported")
    
    return embed


async def get_head_to_head_stats(user1_id, user2_id, user1_display_name=None, user2_display_name=None, guild_id=None):
    """Get head-to-head match statistics between two players."""
    try:
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
                        
                    # Stored names are complete -- see get_member_name's
                    # docstring for why a miss just formats as "User <id>".
                    if user1_display_name == "Unknown":
                        user1_display_name = get_member_name(None, user1_id)
                    if user2_display_name == "Unknown":
                        user2_display_name = get_member_name(None, user2_id)
        
        # Count head-to-head matches and draft-level (teammate/opponent)
        # records from the match-result ledger (the source of truth the
        # rating system already uses) instead of sign_ups JSON, per-draft
        # team_a/team_b queries, and teams_start_time -- scope is
        # RATING_SESSION_TYPES via the ledger fold (LedgerSnapshot), same
        # as /stats.
        def _match_record(h):
            matches_played = h["matches_played"]
            user1_wins = h["matches_won"]
            user2_wins = matches_played - user1_wins
            return {
                "matches_played": matches_played,
                "user1_wins": user1_wins,
                "user2_wins": user2_wins,
                "user1_win_percentage": calculate_win_percentage(user1_wins, user2_wins),
                "user2_win_percentage": calculate_win_percentage(user2_wins, user1_wins),
            }

        def _draft_record(h, kind):
            """kind: 'with' (teammate) or 'against' (opposing)."""
            played = h[f"drafts_{kind}"]
            won = h[f"drafts_{kind}_won"]
            drawn = h[f"drafts_{kind}_tied"]
            losses = played - won - drawn
            return {
                "wins": won,
                "losses": losses,
                "draws": drawn,
                # Team-draft outcome: ties count in the denominator
                # (stats_core owns the formula, same as /stats).
                "win_percentage": calculate_team_draft_win_percentage(won, losses, drawn),
            }

        # One SQL fetch; one pure fold per timeframe (the query doesn't
        # vary by timeframe, so refetching per frame just repeated
        # identical I/O). Timeframe policy comes from stats_core, same as
        # /stats -- lifetime is since=None.
        snapshot = await LedgerSnapshot.fetch(guild_id)
        results = {
            "user1_id": user1_id,
            "user2_id": user2_id,
            "user1_display_name": user1_display_name,
            "user2_display_name": user2_display_name,
        }
        for frame, since in (("lifetime", None),
                             ("monthly", get_timeframe_start_date("month")),
                             ("weekly", get_timeframe_start_date("week"))):
            h = h2h_totals(snapshot.fold(player_id=user1_id, since=since),
                           user2_id)
            results[frame] = _match_record(h)
            results[f"opposing_{frame}"] = _draft_record(h, "against")
            results[f"teammate_{frame}"] = _draft_record(h, "with")
        return results

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
                
                # Add all database matches. PlayerStats display names are
                # complete (dispnamefill0 migration + one-time legacy
                # resolution script; the live signup path keeps them
                # current), so there is no sign_ups-JSON fallback scan --
                # that display artifact carried stale entries.
                for player in players:
                    matches.append((player.player_id, player.display_name))

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