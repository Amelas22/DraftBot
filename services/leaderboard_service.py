from datetime import datetime, timedelta
from loguru import logger
from sqlalchemy import select, and_
from database.db_session import db_session
from helpers.display_names import get_member_name
from models.win_streak_history import WinStreakHistory
from models.perfect_streak_history import PerfectStreakHistory
from models.draft_streak_history import DraftStreakHistory
from models.player import PlayerStats
from models import QuizStats, QuizSubmission, QuizSession
from models.trophy_quiz_submission import TrophyQuizSubmission
from models.trophy_quiz_session import TrophyQuizSession
from stats_core import calculate_win_percentage, calculate_team_draft_win_percentage
from bot_registry import get_bot

# Win Streak minimum requirements by timeframe
STREAK_MINIMUMS = {
    'active': 6,
    '30d': 6,
    '90d': 8,
    'lifetime': 10
}

# Perfect Streak minimum requirements by timeframe
# Higher than regular streaks since 2-0 is harder
PERFECT_STREAK_MINIMUMS = {
    'active': 4,
    '30d': 4,
    '90d': 5,
    'lifetime': 6
}

# Quiz minimum requirements
QUIZ_MINIMUMS = {
    'quizzes': 3  # Minimum quizzes to appear on leaderboard
}

# Order of the White Lotus (draft win streak) minimum requirements
DRAFT_WIN_STREAK_MINIMUMS = {
    'active': 3,
    '30d': 3,
    '90d': 5,
    'lifetime': 8
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

    # Streak/quiz categories are backed by their own dedicated tables
    # (WinStreakHistory, QuizStats, etc) and never touch the match-result
    # ledger fold below (fetch_session_records) -- dispatch to them FIRST
    # so they never pay for a fold whose result they'd throw away; the fold
    # alone measured ~10s on prod-scale data.
    dedicated_query_categories = {
        "longest_win_streak": get_win_streak_leaderboard_data,
        "perfect_streak": get_perfect_streak_leaderboard_data,
        "quiz_points": get_quiz_points_leaderboard_data,
        "trophy_quiz_points": get_trophy_quiz_points_leaderboard_data,
        "draft_win_streak": get_draft_win_streak_leaderboard_data,
    }
    if category in dedicated_query_categories:
        async with db_session() as session:
            sorted_players = await dedicated_query_categories[category](
                guild_id, timeframe, limit, session)
        return sorted_players[:limit]

    # Get the start date for filtering based on timeframe
    start_date = get_timeframe_date(timeframe)

    # Store player stats here
    players_data = {}

    async with db_session() as session:
        # Count from the match-result ledger (the source of truth the rating
        # system already uses) instead of the old sign_ups/team_a/team_b
        # query, which missed premade drafts (wrong session_type filter) and
        # legacy drafts (no victory_message_id_results_channel). Scope is
        # RATING_SESSION_TYPES via fetch_session_records, same as /stats
        # and /record.
        from stats_core import calculate_win_percentage, calculate_team_draft_win_percentage
        from services.ledger_stats import fetch_session_records, match_totals, draft_totals, team_record, side_outcome

        records = await fetch_session_records(guild_id, since=start_date)
        per_player: dict[str, list] = {}
        for r in records:
            per_player.setdefault(r["player_id"], []).append(r)

        logger.info(f"Found {len(per_player)} players with rated drafts in guild {guild_id} for timeframe {timeframe}")

        # Batch-resolve display names: PlayerStats.display_name first (kept
        # in sync as players interact with the bot), get_member_name
        # fallback for players with no PlayerStats row -- never sign_ups
        # JSON, which legacy sessions don't have.
        player_ids = list(per_player.keys())
        name_lookup = {}
        if player_ids:
            names_stmt = select(PlayerStats).where(
                PlayerStats.guild_id == guild_id,
                PlayerStats.player_id.in_(player_ids)
            )
            names_result = await session.execute(names_stmt)
            for p in names_result.scalars().all():
                if p.display_name:
                    name_lookup[p.player_id] = p.display_name

        # Live Discord fallback for players with no stored name anywhere
        # (legacy-only accounts): resolve through the registered bot when
        # available -- None outside a running bot (tests, scripts), where
        # get_member_name degrades to "User <id>" as before.
        _bot = get_bot()
        discord_guild = _bot.get_guild(int(guild_id)) if _bot else None

        for player_id, player_records in per_player.items():
            display_name = name_lookup.get(player_id) or get_member_name(discord_guild, player_id)

            totals = match_totals(player_records)
            matches_played = totals["matches_played"]
            matches_won = totals["matches_won"]
            matches_lost = matches_played - matches_won
            match_win_percentage = calculate_win_percentage(matches_won, matches_lost)

            drafts_played = draft_totals(player_records)

            team = team_record(player_records)
            team_drafts_played = team["played"]
            team_drafts_won = team["won"]
            team_drafts_lost = team["lost"]
            team_drafts_tied = team["tied"]
            # Same tie-inclusive denominator policy as /stats and /record
            # (stats_core owns the formula) -- the old inline formula
            # silently excluded ties from the denominator.
            team_draft_win_percentage = calculate_team_draft_win_percentage(
                team_drafts_won, team_drafts_lost, team_drafts_tied)

            players_data[player_id] = {
                "player_id": player_id,
                "display_name": display_name,
                "drafts_played": drafts_played,
                "completed_matches": matches_played,  # Every reported match counts
                "matches_won": matches_won,
                "matches_lost": matches_lost,
                "match_win_percentage": match_win_percentage,
                "team_drafts_played": team_drafts_played,
                "team_drafts_won": team_drafts_won,
                "team_drafts_tied": team_drafts_tied,
                "team_drafts_lost": team_drafts_lost,
                "team_draft_win_percentage": team_draft_win_percentage,
                "teammate_win_rates": {}
            }

        # Second pass: teammate (Vault/Key) stats. A session's teammates come
        # straight from fetch_session_records' "teammates" set -- same side,
        # computed from team_a/team_b, not "never appeared as an opponent"
        # (that heuristic misclassifies an opposing player you simply never
        # got paired against, e.g. in a 4v4 where 3 rounds only cover 3 of
        # each player's 4 possible opponents). The session's own
        # side_wins/side_losses (same numbers team_record uses) determine
        # won/lost/tied for every teammate at once. Deferred to a second
        # pass so every player's display_name is already resolved above,
        # regardless of dict iteration order.
        for player_id, player_records in per_player.items():
            teammate_stats = players_data[player_id]["teammate_win_rates"]
            for r in player_records:
                if not r["completed"]:
                    continue
                teammates = r["teammates"]
                if not teammates:
                    continue
                outcome = side_outcome(r)
                for teammate_id in teammates:
                    if teammate_id not in players_data:
                        continue
                    entry = teammate_stats.setdefault(teammate_id, {
                        "drafts_played": 0,
                        "drafts_won": 0,
                        "drafts_lost": 0,
                        "drafts_tied": 0,
                        "win_percentage": 0,
                        "teammate_name": players_data[teammate_id]["display_name"]
                    })
                    entry["drafts_played"] += 1
                    entry[f"drafts_{outcome}"] += 1

        # Calculate teammate win rates
        for player_data in players_data.values():
            for teammate_data in player_data["teammate_win_rates"].values():
                teammate_data["win_percentage"] = calculate_team_draft_win_percentage(
                    teammate_data["drafts_won"], teammate_data["drafts_lost"],
                    teammate_data["drafts_tied"])

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

                    # Ties are drafts played together: they count toward the
                    # sample-size gate and the denominator (one tie policy,
                    # already applied where win_percentage was stored above).
                    if teammate_data["drafts_played"] >= min_partnership_drafts:
                        win_percentage = teammate_data["win_percentage"]
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

        # longest_win_streak / perfect_streak / quiz_points / trophy_quiz_points /
        # draft_win_streak are handled by the dedicated_query_categories dispatch
        # above, before the fold ever runs.

        else:
            # Default to drafts_played if category not recognized
            sorted_players = sorted(players_list, key=lambda p: p["drafts_played"], reverse=True)

        # Limit to requested number
        return sorted_players[:limit]


async def _get_ender_players_lookup(guild_id, history_streaks, session):
    """
    Helper to bulk load PlayerStats for players who ended a streak.
    """
    if not history_streaks:
        return {}

    ender_player_ids = list(set(
        s.ended_by_player_id for s in history_streaks
        if s.ended_by_player_id is not None
    ))

    if not ender_player_ids:
        return {}

    enders_bulk_stmt = select(PlayerStats).where(
        PlayerStats.guild_id == guild_id,
        PlayerStats.player_id.in_(ender_player_ids)
    )
    enders_bulk_result = await session.execute(enders_bulk_stmt)
    enders_bulk = enders_bulk_result.scalars().all()
    return {p.player_id: p for p in enders_bulk}


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

        # Bulk load PlayerStats for players who ended these streaks
        enders_lookup = await _get_ender_players_lookup(guild_id, history_streaks, session)
    else:
        players_lookup = {}
        enders_lookup = {}

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
            # Get the display name of the player who ended this streak
            ender_player = enders_lookup.get(streak.ended_by_player_id)
            ended_by_name = ender_player.display_name if ender_player else None

            streak_entries.append({
                "player_id": streak.player_id,
                "display_name": player.display_name,
                "longest_win_streak": streak.streak_length,
                "games_won": player.games_won,
                "games_lost": player.games_lost,
                "completed_matches": player.games_won + player.games_lost,
                "is_active": False,
                "started_at": streak.started_at,
                "ended_at": streak.ended_at,
                "ended_by_player_id": streak.ended_by_player_id,
                "ended_by_name": ended_by_name
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


async def get_perfect_streak_leaderboard_data(guild_id, timeframe, limit, session):
    """
    Get perfect streak (2-0 wins only) leaderboard data.
    Tracks consecutive 2-0 match wins.
    """
    min_streak = PERFECT_STREAK_MINIMUMS.get(timeframe, 8)

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
        history_stmt = select(PerfectStreakHistory).where(
            PerfectStreakHistory.guild_id == guild_id,
            PerfectStreakHistory.ended_at.isnot(None)  # Only completed streaks
        )

        if cutoff_date:
            # Streak must have ENDED within timeframe (recently completed)
            history_stmt = history_stmt.where(
                PerfectStreakHistory.ended_at >= cutoff_date
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

        # Bulk load PlayerStats for players who ended these streaks
        enders_lookup = await _get_ender_players_lookup(guild_id, history_streaks, session)
    else:
        players_lookup = {}
        enders_lookup = {}

    # === Part 2: Get active streaks from PlayerStats ===
    # Active streaks are always included (they're happening NOW)
    # No date filtering needed - if it's active, it's current
    players_stmt = select(PlayerStats).where(
        PlayerStats.guild_id == guild_id,
        PlayerStats.current_perfect_streak > 0
    )

    players_result = await session.execute(players_stmt)
    active_players = players_result.scalars().all()

    # === Part 3: Combine into unified format ===
    streak_entries = []

    # Add completed streaks (using bulk-loaded players)
    for streak in history_streaks:
        player = players_lookup.get(streak.player_id)

        if player and streak.streak_length >= min_streak:
            # Get the display name of the player who ended this streak
            ender_player = enders_lookup.get(streak.ended_by_player_id)
            ended_by_name = ender_player.display_name if ender_player else None

            streak_entries.append({
                "player_id": streak.player_id,
                "display_name": player.display_name,
                "perfect_streak": streak.streak_length,
                "games_won": player.games_won,
                "games_lost": player.games_lost,
                "completed_matches": player.games_won + player.games_lost,
                "is_active": False,
                "started_at": streak.started_at,
                "ended_at": streak.ended_at,
                "ended_by_player_id": streak.ended_by_player_id,
                "ended_by_name": ended_by_name
            })

    # Add active streaks
    for player in active_players:
        if player.current_perfect_streak >= min_streak:
            streak_entries.append({
                "player_id": player.player_id,
                "display_name": player.display_name,
                "perfect_streak": player.current_perfect_streak,
                "games_won": player.games_won,
                "games_lost": player.games_lost,
                "completed_matches": player.games_won + player.games_lost,
                "is_active": True,
                "started_at": player.current_perfect_streak_started_at,
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
            if entry["perfect_streak"] > player_best_streaks[player_id]["perfect_streak"]:
                player_best_streaks[player_id] = entry

    # === Part 5: Sort by streak length, then win % ===
    sorted_players = sorted(
        player_best_streaks.values(),
        key=lambda p: (
            p["perfect_streak"],
            p["games_won"] / p["completed_matches"] if p["completed_matches"] > 0 else 0
        ),
        reverse=True
    )

    # Apply limit
    return sorted_players[:limit]


async def get_draft_win_streak_leaderboard_data(guild_id, timeframe, limit, session):
    """
    Get Order of the White Lotus (draft win streak) leaderboard data.
    Tracks consecutive draft wins (losses break, ties continue).
    """
    min_streak = DRAFT_WIN_STREAK_MINIMUMS.get(timeframe, 8)

    # Calculate date cutoff
    if timeframe == "active":
        cutoff_date = None
    elif timeframe == "lifetime":
        cutoff_date = None
    elif timeframe == "90d":
        cutoff_date = datetime.now() - timedelta(days=90)
    elif timeframe == "30d":
        cutoff_date = datetime.now() - timedelta(days=30)
    else:
        cutoff_date = None

    # Get completed streaks from history
    if timeframe == "active":
        history_streaks = []
    else:
        history_stmt = select(DraftStreakHistory).where(
            DraftStreakHistory.guild_id == guild_id,
            DraftStreakHistory.ended_at.isnot(None)
        )

        if cutoff_date:
            history_stmt = history_stmt.where(
                DraftStreakHistory.ended_at >= cutoff_date
            )

        history_result = await session.execute(history_stmt)
        history_streaks = history_result.scalars().all()

    # Bulk load PlayerStats for history entries
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

    # Get active streaks
    players_stmt = select(PlayerStats).where(
        PlayerStats.guild_id == guild_id,
        PlayerStats.current_draft_win_streak > 0
    )
    players_result = await session.execute(players_stmt)
    active_players = players_result.scalars().all()

    # Combine into unified format
    streak_entries = []

    # Add completed streaks
    for streak in history_streaks:
        player = players_lookup.get(streak.player_id)
        if player and streak.streak_length >= min_streak:
            streak_entries.append({
                "player_id": streak.player_id,
                "display_name": player.display_name,
                "draft_win_streak": streak.streak_length,
                "team_drafts_won": player.team_drafts_won,
                "is_active": False,
                "started_at": streak.started_at,
                "ended_at": streak.ended_at
            })

    # Add active streaks
    for player in active_players:
        if player.current_draft_win_streak >= min_streak:
            streak_entries.append({
                "player_id": player.player_id,
                "display_name": player.display_name,
                "draft_win_streak": player.current_draft_win_streak,
                "team_drafts_won": player.team_drafts_won,
                "is_active": True,
                "started_at": player.current_draft_win_streak_started_at,
                "ended_at": None
            })

    # Deduplicate - keep best per player
    player_best_streaks = {}
    for entry in streak_entries:
        player_id = entry["player_id"]
        if player_id not in player_best_streaks:
            player_best_streaks[player_id] = entry
        else:
            if entry["draft_win_streak"] > player_best_streaks[player_id]["draft_win_streak"]:
                player_best_streaks[player_id] = entry

    # Sort by streak length, then total drafts won
    sorted_players = sorted(
        player_best_streaks.values(),
        key=lambda p: (p["draft_win_streak"], p["team_drafts_won"]),
        reverse=True
    )

    return sorted_players[:limit]


async def get_quiz_points_leaderboard_data(guild_id, timeframe, limit, session):
    """
    Get quiz points leaderboard data with true time-based point aggregation.
    Ranks players by total quiz points earned within the timeframe.
    """
    min_quizzes = QUIZ_MINIMUMS['quizzes']

    # Calculate date cutoff for timeframe
    if timeframe == "lifetime":
        cutoff_date = None
    elif timeframe == "90d":
        cutoff_date = datetime.now() - timedelta(days=90)
    elif timeframe == "30d":
        cutoff_date = datetime.now() - timedelta(days=30)
    elif timeframe == "14d":
        cutoff_date = datetime.now() - timedelta(days=14)
    else:
        cutoff_date = None

    if cutoff_date:
        # Time-based filtering: Query QuizSubmission and aggregate
        # Join through QuizSession to filter by guild
        stmt = select(QuizSubmission).join(
            QuizSession,
            QuizSubmission.quiz_id == QuizSession.quiz_id
        ).where(
            and_(
                QuizSession.guild_id == str(guild_id),
                QuizSubmission.submitted_at >= cutoff_date
            )
        )

        result = await session.execute(stmt)
        submissions = result.scalars().all()

        # Aggregate by player
        player_aggregates = {}
        for sub in submissions:
            if sub.player_id not in player_aggregates:
                player_aggregates[sub.player_id] = {
                    "player_id": sub.player_id,
                    "display_name": sub.display_name,
                    "total_points": 0,
                    "total_quizzes": 0,
                    "total_picks_correct": 0,
                    "total_picks_attempted": 0,
                    "highest_quiz_score": 0
                }

            agg = player_aggregates[sub.player_id]
            agg["total_points"] += sub.points_earned
            agg["total_quizzes"] += 1
            agg["total_picks_correct"] += sub.correct_count
            agg["total_picks_attempted"] += 4
            if sub.points_earned > agg["highest_quiz_score"]:
                agg["highest_quiz_score"] = sub.points_earned

        # Calculate derived stats and filter by minimum
        leaderboard_data = []
        for player_id, agg in player_aggregates.items():
            if agg["total_quizzes"] >= min_quizzes:
                agg["average_points_per_quiz"] = agg["total_points"] / agg["total_quizzes"]
                agg["accuracy_percentage"] = (agg["total_picks_correct"] / agg["total_picks_attempted"] * 100) if agg["total_picks_attempted"] > 0 else 0
                # Note: Streaks aren't time-scoped, so we don't include them for time-based views
                agg["current_perfect_streak"] = 0
                agg["longest_perfect_streak"] = 0
                leaderboard_data.append(agg)

        # Sort by total points
        leaderboard_data.sort(key=lambda p: p["total_points"], reverse=True)

    else:
        # Lifetime: Use QuizStats (already aggregated)
        stmt = select(QuizStats).where(
            QuizStats.guild_id == guild_id,
            QuizStats.total_quizzes >= min_quizzes
        ).order_by(QuizStats.total_points.desc())

        result = await session.execute(stmt)
        quiz_stats = result.scalars().all()

        # Convert to leaderboard format
        leaderboard_data = []
        for stats in quiz_stats:
            leaderboard_data.append({
                "player_id": stats.player_id,
                "display_name": stats.display_name,
                "total_points": stats.total_points,
                "total_quizzes": stats.total_quizzes,
                "accuracy_percentage": stats.accuracy_percentage,
                "average_points_per_quiz": stats.average_points_per_quiz,
                "highest_quiz_score": stats.highest_quiz_score,
                "current_perfect_streak": stats.current_perfect_streak,
                "longest_perfect_streak": stats.longest_perfect_streak
            })

    # Apply limit
    return leaderboard_data[:limit]


async def get_trophy_quiz_points_leaderboard_data(guild_id, timeframe, limit, session):
    """Rank players by summed TrophyQuizSubmission.points_earned in the guild.
    Aggregates submissions directly for all timeframes; separate from the pick
    quiz's quiz_points leaderboard."""
    # Calculate date cutoff for timeframe
    if timeframe == "90d":
        cutoff_date = datetime.now() - timedelta(days=90)
    elif timeframe == "30d":
        cutoff_date = datetime.now() - timedelta(days=30)
    elif timeframe == "14d":
        cutoff_date = datetime.now() - timedelta(days=14)
    else:
        cutoff_date = None

    stmt = select(TrophyQuizSubmission).join(
        TrophyQuizSession,
        TrophyQuizSubmission.quiz_id == TrophyQuizSession.quiz_id
    ).where(
        TrophyQuizSession.guild_id == str(guild_id),
        # Only committed answers count; a pending (unfinalized) row is a player who
        # submitted an initial guess but never chose Keep or Pay-to-change.
        TrophyQuizSubmission.finalized.is_(True),
    )
    if cutoff_date is not None:
        stmt = stmt.where(TrophyQuizSubmission.submitted_at >= cutoff_date)

    submissions = (await session.execute(stmt)).scalars().all()

    player_aggregates = {}
    for sub in submissions:
        agg = player_aggregates.setdefault(sub.player_id, {
            "player_id": sub.player_id,
            "display_name": sub.display_name,
            "total_points": 0,
            "total_quizzes": 0
        })
        agg["total_points"] += sub.points_earned
        agg["total_quizzes"] += 1

    leaderboard_data = sorted(player_aggregates.values(), key=lambda p: p["total_points"], reverse=True)
    return leaderboard_data[:limit]


async def get_crown_leaders(guild_id: str, categories: list, timeframe: str = "lifetime") -> dict:
    """
    Get the Discord user ID(s) of the #1 player(s) for each specified category.

    Note: time_vault_and_key returns BOTH player_id and teammate_id since
    both partners in the #1 duo deserve a crown.

    Args:
        guild_id: The guild to get leaders for
        categories: List of category names to check
        timeframe: The timeframe to use for leaderboard queries

    Returns:
        dict mapping category -> list of player_ids (empty list if none qualify)
    """
    leaders = {}
    for category in categories:
        # Reuse existing get_leaderboard_data() with limit=1
        data = await get_leaderboard_data(guild_id, category=category, limit=1, timeframe=timeframe)
        if data and len(data) > 0:
            first_entry = data[0]
            if category == "time_vault_and_key":
                # Partnership leaderboard - both players get credit
                leaders[category] = [first_entry.get('player_id'), first_entry.get('teammate_id')]
            else:
                leaders[category] = [first_entry.get('player_id')]
        else:
            leaders[category] = []
    return leaders


def calculate_crown_counts(leaders: dict) -> dict:
    """
    Given category -> list of player_ids mapping, return player_id -> crown_count.

    Args:
        leaders: dict mapping category name to list of player IDs holding #1

    Returns:
        dict mapping player_id -> number of crowns they hold
    """
    crown_counts = {}
    for category, player_ids in leaders.items():
        for player_id in player_ids:
            if player_id:
                crown_counts[player_id] = crown_counts.get(player_id, 0) + 1
    return crown_counts