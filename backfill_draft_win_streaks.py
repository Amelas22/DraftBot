#!/usr/bin/env python3
"""
Backfill script for Order of the White Lotus (draft win streaks)

This script recalculates all draft win streak data from scratch based on historical draft results.

It performs the following actions:
1. Calculates team_drafts_won/lost/tied from historical draft data
2. Optionally calculates current and longest draft win streaks from historical data
3. Optionally populates DraftStreakHistory with historical streaks
4. Clears and rebuilds all streak history to ensure accuracy

Usage:
    pipenv run python backfill_draft_win_streaks.py [--calculate-streaks] [--dry-run]

Options:
    --calculate-streaks    Also calculate historical longest streaks AND current streaks (takes longer)
    --dry-run             Show what would be done without making changes

Example:
    # Dry run to see what would be fixed:
    pipenv run python backfill_draft_win_streaks.py --calculate-streaks --dry-run

    # Actually fix the data:
    pipenv run python backfill_draft_win_streaks.py --calculate-streaks
"""

import asyncio
import argparse
from datetime import datetime
from sqlalchemy import select
from session import AsyncSessionLocal, DraftSession, MatchResult, PlayerStats
from models.draft_streak_history import DraftStreakHistory
from loguru import logger

# Configure logger
logger.add("backfill_draft_streaks.log", rotation="10 MB")


async def get_all_completed_drafts(session):
    """Get all completed drafts ordered by completion time"""
    stmt = select(DraftSession).where(
        DraftSession.victory_message_id_results_channel.isnot(None),
        DraftSession.session_type.in_(["random", "staked", "premade"])
    ).order_by(DraftSession.teams_start_time)

    result = await session.execute(stmt)
    return result.scalars().all()


async def calculate_draft_result(session, draft_session):
    """Calculate the result of a draft (team_a wins, team_b wins, is_tie)"""
    stmt = select(MatchResult).where(
        MatchResult.session_id == draft_session.session_id,
        MatchResult.winner_id.isnot(None)
    )
    result = await session.execute(stmt)
    matches = result.scalars().all()

    team_a_wins = 0
    team_b_wins = 0

    for match in matches:
        if match.winner_id in draft_session.team_a:
            team_a_wins += 1
        elif match.winner_id in draft_session.team_b:
            team_b_wins += 1

    if team_a_wins > team_b_wins:
        return "team_a", team_a_wins, team_b_wins
    elif team_b_wins > team_a_wins:
        return "team_b", team_a_wins, team_b_wins
    else:
        return "tie", team_a_wins, team_b_wins


async def backfill_draft_counts(dry_run=False):
    """Backfill team_drafts_won/lost/tied from historical data"""
    logger.info("Starting backfill of draft win/loss/tie counts...")

    # Step 1: Read all drafts and calculate stats
    async with AsyncSessionLocal() as session:
        # Get all completed drafts
        drafts = await get_all_completed_drafts(session)
        logger.info(f"Found {len(drafts)} completed drafts to process")

        # Track stats per player per guild
        player_stats = {}  # {(player_id, guild_id): {"won": 0, "lost": 0, "tied": 0}}

        for i, draft in enumerate(drafts, 1):
            if i % 100 == 0:
                logger.info(f"Processing draft {i}/{len(drafts)}...")

            result, team_a_wins, team_b_wins = await calculate_draft_result(session, draft)
            guild_id = draft.guild_id

            # Update counts for all players
            for player_id in draft.team_a:
                key = (player_id, guild_id)
                if key not in player_stats:
                    player_stats[key] = {"won": 0, "lost": 0, "tied": 0}

                if result == "team_a":
                    player_stats[key]["won"] += 1
                elif result == "team_b":
                    player_stats[key]["lost"] += 1
                else:
                    player_stats[key]["tied"] += 1

            for player_id in draft.team_b:
                key = (player_id, guild_id)
                if key not in player_stats:
                    player_stats[key] = {"won": 0, "lost": 0, "tied": 0}

                if result == "team_b":
                    player_stats[key]["won"] += 1
                elif result == "team_a":
                    player_stats[key]["lost"] += 1
                else:
                    player_stats[key]["tied"] += 1

        logger.info(f"Calculated stats for {len(player_stats)} player/guild combinations")

    # Step 2: Update database in a new session
    if not dry_run:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                updated_count = 0
                for (player_id, guild_id), stats in player_stats.items():
                    stmt = select(PlayerStats).where(
                        PlayerStats.player_id == player_id,
                        PlayerStats.guild_id == guild_id
                    )
                    player_stat = await session.scalar(stmt)

                    if player_stat:
                        player_stat.team_drafts_won = stats["won"]
                        player_stat.team_drafts_lost = stats["lost"]
                        player_stat.team_drafts_tied = stats["tied"]
                        updated_count += 1
                    else:
                        logger.warning(f"PlayerStats not found for player {player_id} in guild {guild_id}")

                await session.commit()
                logger.info(f"Updated {updated_count} player records")
    else:
        logger.info("DRY RUN - Would update the following:")
        for (player_id, guild_id), stats in list(player_stats.items())[:10]:
            logger.info(f"  Player {player_id} in guild {guild_id}: {stats}")
        logger.info(f"  ... and {len(player_stats) - 10} more")


async def backfill_historical_streaks(dry_run=False):
    """Calculate and store historical longest streaks"""
    logger.info("Starting backfill of historical longest streaks...")

    # Step 1: Read all drafts and calculate streaks
    async with AsyncSessionLocal() as session:
        # Get all completed drafts ordered by time
        drafts = await get_all_completed_drafts(session)
        logger.info(f"Found {len(drafts)} completed drafts to process for streaks")

        # Track streaks per player per guild
        player_streaks = {}  # {(player_id, guild_id): {"current": 0, "longest": 0, "started_at": None, "current_started_at": None}}
        historical_streaks = []  # List of streaks to save to history

        for i, draft in enumerate(drafts, 1):
            if i % 100 == 0:
                logger.info(f"Processing draft {i}/{len(drafts)} for streaks...")

            result, team_a_wins, team_b_wins = await calculate_draft_result(session, draft)
            guild_id = draft.guild_id
            draft_time = draft.teams_start_time or datetime.now()

            # Process team A
            for player_id in draft.team_a:
                key = (player_id, guild_id)
                if key not in player_streaks:
                    player_streaks[key] = {"current": 0, "longest": 0, "started_at": None, "current_started_at": None}

                if result == "team_a":  # Win
                    if player_streaks[key]["current"] == 0:
                        player_streaks[key]["started_at"] = draft_time
                        player_streaks[key]["current_started_at"] = draft_time
                    player_streaks[key]["current"] += 1
                    if player_streaks[key]["current"] > player_streaks[key]["longest"]:
                        player_streaks[key]["longest"] = player_streaks[key]["current"]
                elif result == "tie":  # Tie - maintain streak
                    pass
                else:  # Loss - break streak
                    if player_streaks[key]["current"] > 0:
                        # Record historical streak
                        historical_streaks.append({
                            "player_id": player_id,
                            "guild_id": guild_id,
                            "streak_length": player_streaks[key]["current"],
                            "started_at": player_streaks[key]["started_at"],
                            "ended_at": draft_time
                        })
                    player_streaks[key]["current"] = 0
                    player_streaks[key]["started_at"] = None
                    player_streaks[key]["current_started_at"] = None

            # Process team B
            for player_id in draft.team_b:
                key = (player_id, guild_id)
                if key not in player_streaks:
                    player_streaks[key] = {"current": 0, "longest": 0, "started_at": None, "current_started_at": None}

                if result == "team_b":  # Win
                    if player_streaks[key]["current"] == 0:
                        player_streaks[key]["started_at"] = draft_time
                        player_streaks[key]["current_started_at"] = draft_time
                    player_streaks[key]["current"] += 1
                    if player_streaks[key]["current"] > player_streaks[key]["longest"]:
                        player_streaks[key]["longest"] = player_streaks[key]["current"]
                elif result == "tie":  # Tie - maintain streak
                    pass
                else:  # Loss - break streak
                    if player_streaks[key]["current"] > 0:
                        # Record historical streak
                        historical_streaks.append({
                            "player_id": player_id,
                            "guild_id": guild_id,
                            "streak_length": player_streaks[key]["current"],
                            "started_at": player_streaks[key]["started_at"],
                            "ended_at": draft_time
                        })
                    player_streaks[key]["current"] = 0
                    player_streaks[key]["started_at"] = None
                    player_streaks[key]["current_started_at"] = None

        logger.info(f"Calculated {len(historical_streaks)} historical streaks")
        logger.info(f"Found longest streaks for {len(player_streaks)} players")

    # Step 2: Update database in a new session
    if not dry_run:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # First, clear existing history to avoid duplicates
                logger.info("Clearing existing draft streak history...")
                from sqlalchemy import delete
                delete_stmt = delete(DraftStreakHistory)
                await session.execute(delete_stmt)

                # Update longest_draft_win_streak AND current_draft_win_streak
                updated_count = 0
                for (player_id, guild_id), streak_data in player_streaks.items():
                    stmt = select(PlayerStats).where(
                        PlayerStats.player_id == player_id,
                        PlayerStats.guild_id == guild_id
                    )
                    player_stat = await session.scalar(stmt)

                    if player_stat:
                        player_stat.longest_draft_win_streak = streak_data["longest"]
                        player_stat.current_draft_win_streak = streak_data["current"]
                        player_stat.current_draft_win_streak_started_at = streak_data["current_started_at"]
                        updated_count += 1
                    else:
                        logger.warning(f"PlayerStats not found for player {player_id} in guild {guild_id}")

                # Insert historical streaks (only those >= 3)
                significant_streaks = [s for s in historical_streaks if s["streak_length"] >= 3]
                logger.info(f"Inserting {len(significant_streaks)} significant historical streaks (>= 3)")

                for streak in significant_streaks:
                    history_entry = DraftStreakHistory(**streak)
                    session.add(history_entry)

                await session.commit()
                logger.info(f"Updated {updated_count} player records with current and longest streaks")
                logger.info(f"Inserted {len(significant_streaks)} historical streak records")
    else:
        logger.info("DRY RUN - Would update the following:")
        for (player_id, guild_id), streak_data in list(player_streaks.items())[:10]:
            logger.info(f"  Player {player_id} in guild {guild_id}: current={streak_data['current']}, longest={streak_data['longest']}")
        logger.info(f"  ... and {len(player_streaks) - 10} more")
        logger.info(f"Would insert {len([s for s in historical_streaks if s['streak_length'] >= 3])} historical streaks")


async def main():
    parser = argparse.ArgumentParser(description="Backfill Order of the White Lotus draft win streak data")
    parser.add_argument("--calculate-streaks", action="store_true",
                       help="Also calculate historical longest streaks (takes longer)")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show what would be done without making changes")

    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Order of the White Lotus - Backfill Script")
    logger.info("=" * 60)

    if args.dry_run:
        logger.info("DRY RUN MODE - No changes will be made")

    # Always backfill draft counts (fast)
    await backfill_draft_counts(dry_run=args.dry_run)

    # Optionally calculate historical streaks (slower)
    if args.calculate_streaks:
        logger.info("")
        await backfill_historical_streaks(dry_run=args.dry_run)
    else:
        logger.info("")
        logger.info("Skipping historical streak calculation (use --calculate-streaks to enable)")

    logger.info("")
    logger.info("=" * 60)
    logger.info("Backfill complete!")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
