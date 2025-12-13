#!/usr/bin/env python3
"""One-time backfill script to build win streak history from match records."""
import asyncio
import sys
from pathlib import Path
from datetime import datetime
from sqlalchemy import select, and_, or_

# Add parent directory to path to import project modules
sys.path.append(str(Path(__file__).parent.parent))

from session import AsyncSessionLocal
from models.player import PlayerStats
from models.match import MatchResult
from models.draft_session import DraftSession
from models.win_streak_history import WinStreakHistory


async def backfill_player_streaks(player_id, guild_id, session):
    """Reconstruct all streaks for a single player."""
    from sqlalchemy import delete

    # Step 1: Delete all existing streak history for this player
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
            continue  # Draw

        match_date = draft_session.teams_start_time

        if match_result.winner_id == player_id:
            # Win
            if current_streak == 0:
                streak_start_date = match_date
            current_streak += 1
            longest_lifetime = max(longest_lifetime, current_streak)
        else:
            # Loss
            if current_streak > 0:
                # Record completed streak
                streak_record = WinStreakHistory(
                    player_id=player_id,
                    guild_id=guild_id,
                    streak_length=current_streak,
                    started_at=streak_start_date,
                    ended_at=match_date
                )
                session.add(streak_record)

            current_streak = 0
            streak_start_date = None

    # Step 4: Update PlayerStats with current and longest streaks
    player_stmt = select(PlayerStats).where(
        and_(
            PlayerStats.player_id == player_id,
            PlayerStats.guild_id == guild_id
        )
    )
    player_result = await session.execute(player_stmt)
    player = player_result.scalar_one_or_none()

    if player:
        player.current_win_streak = current_streak
        player.current_win_streak_started_at = streak_start_date
        player.longest_win_streak = longest_lifetime

    return longest_lifetime


async def backfill_all():
    """Main backfill function."""
    print("🔧 Starting win streak history backfill...")

    async with AsyncSessionLocal() as session:
        stmt = select(PlayerStats)
        result = await session.execute(stmt)
        all_players = result.scalars().all()

        total = len(all_players)
        print(f"📊 Found {total} players to process")

        for i, player in enumerate(all_players, 1):
            longest = await backfill_player_streaks(
                player.player_id,
                player.guild_id,
                session
            )

            if i % 25 == 0:
                print(f"  ✓ Processed {i}/{total} players...")
                await session.commit()

        await session.commit()

        # Verify results
        count_stmt = select(WinStreakHistory)
        count_result = await session.execute(count_stmt)
        streak_count = len(count_result.scalars().all())

        print(f"\n✅ Backfill complete!")
        print(f"   - {total} players processed")
        print(f"   - {streak_count} historical streaks recorded")


if __name__ == "__main__":
    asyncio.run(backfill_all())
