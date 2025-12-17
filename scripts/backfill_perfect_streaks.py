#!/usr/bin/env python3
"""One-time backfill script to build perfect streak (2-0 wins) history from match records."""
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
from models.perfect_streak_history import PerfectStreakHistory


async def backfill_player_perfect_streaks(player_id, guild_id, session):
    """Reconstruct all perfect streaks for a single player."""
    from sqlalchemy import delete

    # Step 1: Delete all existing perfect streak history for this player
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
    current_streak = 0
    streak_start_date = None
    longest_lifetime = 0

    for match_result, draft_session in matches:
        if match_result.winner_id is None:
            # Draw - breaks perfect streak
            if current_streak > 0:
                # Record completed streak
                streak_record = PerfectStreakHistory(
                    player_id=player_id,
                    guild_id=guild_id,
                    streak_length=current_streak,
                    started_at=streak_start_date,
                    ended_at=match_date
                )
                session.add(streak_record)

            current_streak = 0
            streak_start_date = None
            continue

        match_date = draft_session.teams_start_time

        # Determine if this player won 2-0
        if match_result.winner_id == player_id:
            # Player won - check if it was 2-0
            if match_result.player1_id == player_id:
                player_wins = match_result.player1_wins
                opponent_wins = match_result.player2_wins
            else:
                player_wins = match_result.player2_wins
                opponent_wins = match_result.player1_wins

            is_perfect_win = (player_wins == 2 and opponent_wins == 0)

            if is_perfect_win:
                # Perfect win (2-0) - continue/start streak
                if current_streak == 0:
                    streak_start_date = match_date
                current_streak += 1
                longest_lifetime = max(longest_lifetime, current_streak)
            else:
                # Non-perfect win (2-1) - breaks perfect streak
                if current_streak > 0:
                    # Record completed streak
                    streak_record = PerfectStreakHistory(
                        player_id=player_id,
                        guild_id=guild_id,
                        streak_length=current_streak,
                        started_at=streak_start_date,
                        ended_at=match_date
                    )
                    session.add(streak_record)

                current_streak = 0
                streak_start_date = None
        else:
            # Loss - breaks perfect streak
            if current_streak > 0:
                # Record completed streak
                streak_record = PerfectStreakHistory(
                    player_id=player_id,
                    guild_id=guild_id,
                    streak_length=current_streak,
                    started_at=streak_start_date,
                    ended_at=match_date
                )
                session.add(streak_record)

            current_streak = 0
            streak_start_date = None

    # Step 4: Update PlayerStats with current and longest perfect streaks
    player_stmt = select(PlayerStats).where(
        and_(
            PlayerStats.player_id == player_id,
            PlayerStats.guild_id == guild_id
        )
    )
    player_result = await session.execute(player_stmt)
    player = player_result.scalar_one_or_none()

    if player:
        player.current_perfect_streak = current_streak
        player.current_perfect_streak_started_at = streak_start_date
        player.longest_perfect_streak = longest_lifetime

    return longest_lifetime


async def backfill_all():
    """Main backfill function."""
    print("🔧 Starting perfect streak (2-0 wins) history backfill...")

    async with AsyncSessionLocal() as session:
        stmt = select(PlayerStats)
        result = await session.execute(stmt)
        all_players = result.scalars().all()

        total = len(all_players)
        print(f"📊 Found {total} players to process")

        for i, player in enumerate(all_players, 1):
            longest = await backfill_player_perfect_streaks(
                player.player_id,
                player.guild_id,
                session
            )

            if i % 25 == 0:
                print(f"  ✓ Processed {i}/{total} players...")
                await session.commit()

        await session.commit()

        # Verify results
        count_stmt = select(PerfectStreakHistory)
        count_result = await session.execute(count_stmt)
        streak_count = len(count_result.scalars().all())

        print(f"\n✅ Perfect streak backfill complete!")
        print(f"   - {total} players processed")
        print(f"   - {streak_count} historical perfect streaks recorded")
        print(f"\nNote: Perfect streaks only count consecutive 2-0 wins.")
        print(f"      Any other result (2-1 win, loss, draw) breaks the streak.")


if __name__ == "__main__":
    asyncio.run(backfill_all())
