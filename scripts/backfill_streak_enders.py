#!/usr/bin/env python3
"""
Backfill script to rebuild streak history with ended_by_player_id populated.
This script deletes and rebuilds streak records from match history to ensure accuracy.
"""
import asyncio
import sys
from pathlib import Path
from datetime import datetime
from sqlalchemy import select, and_, or_, delete, func

# Add parent directory to path to import project modules
sys.path.append(str(Path(__file__).parent.parent))

from session import AsyncSessionLocal
from models.player import PlayerStats
from models.match import MatchResult
from models.draft_session import DraftSession
from models.win_streak_history import WinStreakHistory
from models.perfect_streak_history import PerfectStreakHistory
from loguru import logger


async def backfill_player_win_streaks(player_id, guild_id, session):
    """Rebuild win streak history for a single player with ended_by_player_id."""

    # Step 1: Delete existing win streak history
    delete_stmt = delete(WinStreakHistory).where(
        and_(
            WinStreakHistory.player_id == player_id,
            WinStreakHistory.guild_id == guild_id
        )
    )
    await session.execute(delete_stmt)

    # Step 2: Get all matches chronologically
    stmt = (
        select(MatchResult, DraftSession)
        .join(DraftSession, MatchResult.session_id == DraftSession.session_id)
        .where(
            and_(
                DraftSession.guild_id == guild_id,
                DraftSession.session_type.in_(['random', 'staked']),
                or_(
                    MatchResult.player1_id == player_id,
                    MatchResult.player2_id == player_id
                )
            )
        )
        .order_by(DraftSession.teams_start_time, MatchResult.match_number)
    )

    result = await session.execute(stmt)
    matches = result.all()

    # Step 3: Track streak state and rebuild history
    current_streak = 0
    streak_start_date = None
    longest_lifetime = 0

    for match_result, draft_session in matches:
        if match_result.winner_id is None:
            continue  # Draw - doesn't affect win streaks

        match_date = draft_session.teams_start_time

        if match_result.winner_id == player_id:
            # Win - continue/start streak
            if current_streak == 0:
                streak_start_date = match_date
            current_streak += 1
            longest_lifetime = max(longest_lifetime, current_streak)
        else:
            # Loss - end streak
            if current_streak > 0:
                # Record completed streak with who ended it
                opponent_id = match_result.winner_id
                streak_record = WinStreakHistory(
                    player_id=player_id,
                    guild_id=guild_id,
                    streak_length=current_streak,
                    started_at=streak_start_date,
                    ended_at=match_date,
                    ended_by_player_id=opponent_id
                )
                session.add(streak_record)

            # Reset streak
            current_streak = 0
            streak_start_date = None

    # Don't record active streaks (current_streak > 0 at end)
    # Those are tracked in PlayerStats, not history

    return longest_lifetime


async def backfill_player_perfect_streaks(player_id, guild_id, session):
    """Rebuild perfect streak history for a single player with ended_by_player_id."""

    # Step 1: Delete existing perfect streak history
    delete_stmt = delete(PerfectStreakHistory).where(
        and_(
            PerfectStreakHistory.player_id == player_id,
            PerfectStreakHistory.guild_id == guild_id
        )
    )
    await session.execute(delete_stmt)

    # Step 2: Get all matches chronologically
    stmt = (
        select(MatchResult, DraftSession)
        .join(DraftSession, MatchResult.session_id == DraftSession.session_id)
        .where(
            and_(
                DraftSession.guild_id == guild_id,
                DraftSession.session_type.in_(['random', 'staked']),
                or_(
                    MatchResult.player1_id == player_id,
                    MatchResult.player2_id == player_id
                )
            )
        )
        .order_by(DraftSession.teams_start_time, MatchResult.match_number)
    )

    result = await session.execute(stmt)
    matches = result.all()

    # Step 3: Track streak state and rebuild history
    current_perfect_streak = 0
    perfect_streak_start_date = None
    longest_perfect_lifetime = 0

    for match_result, draft_session in matches:
        match_date = draft_session.teams_start_time

        # Determine scores
        if match_result.player1_id == player_id:
            player_wins = match_result.player1_wins
            opponent_wins = match_result.player2_wins
            opponent_id = match_result.player2_id
        else:
            player_wins = match_result.player2_wins
            opponent_wins = match_result.player1_wins
            opponent_id = match_result.player1_id

        is_perfect_win = (player_wins == 2 and opponent_wins == 0)
        player_won = match_result.winner_id == player_id

        if is_perfect_win:
            # Perfect win - continue/start perfect streak
            if current_perfect_streak == 0:
                perfect_streak_start_date = match_date
            current_perfect_streak += 1
            longest_perfect_lifetime = max(longest_perfect_lifetime, current_perfect_streak)
        else:
            # Any other result breaks perfect streak
            if current_perfect_streak > 0:
                # Record completed perfect streak with who ended it
                # Who ended it depends on the match result
                if player_won:
                    # Player won 2-1 - opponent ended the perfect streak by taking a game
                    ender_id = opponent_id
                else:
                    # Player lost - opponent ended the perfect streak by winning
                    ender_id = match_result.winner_id if match_result.winner_id else None

                if ender_id:  # Only record if we have an ender (not a draw)
                    streak_record = PerfectStreakHistory(
                        player_id=player_id,
                        guild_id=guild_id,
                        streak_length=current_perfect_streak,
                        started_at=perfect_streak_start_date,
                        ended_at=match_date,
                        ended_by_player_id=ender_id
                    )
                    session.add(streak_record)

            # Reset perfect streak
            current_perfect_streak = 0
            perfect_streak_start_date = None

    # Don't record active perfect streaks
    # Those are tracked in PlayerStats, not history

    return longest_perfect_lifetime


async def backfill_all_streaks():
    """Rebuild all streak history records with ended_by_player_id."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Get all unique player/guild combinations that have matches
            # Fetch player1 IDs
            stmt1 = (
                select(MatchResult.player1_id.label('player_id'), DraftSession.guild_id)
                .join(DraftSession, MatchResult.session_id == DraftSession.session_id)
                .where(DraftSession.session_type.in_(['random', 'staked']))
            )
            result1 = await session.execute(stmt1)
            player_guilds_1 = result1.all()

            # Fetch player2 IDs
            stmt2 = (
                select(MatchResult.player2_id.label('player_id'), DraftSession.guild_id)
                .join(DraftSession, MatchResult.session_id == DraftSession.session_id)
                .where(DraftSession.session_type.in_(['random', 'staked']))
            )
            result2 = await session.execute(stmt2)
            player_guilds_2 = result2.all()

            # Combine and deduplicate
            player_guilds = list(set(player_guilds_1 + player_guilds_2))

            logger.info(f"Found {len(player_guilds)} player/guild combinations to process")

            win_streak_count = 0
            perfect_streak_count = 0

            for i, (player_id, guild_id) in enumerate(player_guilds, 1):
                if i % 10 == 0:
                    logger.info(f"Processing player {i}/{len(player_guilds)}...")

                # Rebuild win streaks
                await backfill_player_win_streaks(player_id, guild_id, session)

                # Rebuild perfect streaks
                await backfill_player_perfect_streaks(player_id, guild_id, session)

                # Flush periodically
                if i % 50 == 0:
                    await session.flush()

            # Count final records before committing
            win_count_stmt = select(func.count()).select_from(WinStreakHistory)
            win_result = await session.execute(win_count_stmt)
            win_streak_count = win_result.scalar()

            perfect_count_stmt = select(func.count()).select_from(PerfectStreakHistory)
            perfect_result = await session.execute(perfect_count_stmt)
            perfect_streak_count = perfect_result.scalar()

            await session.commit()

            logger.info(f"Rebuilt {win_streak_count} win streak records")
            logger.info(f"Rebuilt {perfect_streak_count} perfect streak records")


async def main():
    """Main entry point for backfill script."""
    logger.info("Starting streak history rebuild with ended_by_player_id...")
    logger.info("=" * 60)
    logger.info("This will DELETE and REBUILD all streak history records")
    logger.info("=" * 60)

    await backfill_all_streaks()

    logger.info("\n" + "=" * 60)
    logger.info("Backfill complete!")


if __name__ == "__main__":
    asyncio.run(main())
