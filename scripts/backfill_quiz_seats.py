#!/usr/bin/env python3
"""One-time backfill script to populate starting_seat for existing quizzes."""
import asyncio
import sys
from pathlib import Path
from sqlalchemy import select, update

# Add parent directory to path to import project modules
sys.path.append(str(Path(__file__).parent.parent))

from session import AsyncSessionLocal
from models import QuizSession, DraftSession
from services.draft_analysis import DraftAnalysis


async def backfill_quiz_seat(quiz: QuizSession, session) -> bool:
    """
    Backfill the starting_seat for a single quiz.

    Determines the seat by:
    1. Loading the pack_trace_data to get the first player name
    2. Loading the DraftAnalysis for that quiz's draft
    3. Finding which seat that player was at

    Returns True if successful, False otherwise.
    """
    try:
        # Get first player name from pack_trace_data
        pack_trace_data = quiz.pack_trace_data
        if not pack_trace_data or not pack_trace_data.get("picks"):
            print(f"  ⚠ Quiz {quiz.quiz_id}: No pack_trace_data")
            return False

        first_player_name = pack_trace_data["picks"][0].get("user_name")
        if not first_player_name:
            print(f"  ⚠ Quiz {quiz.quiz_id}: No first player name in pack_trace_data")
            return False

        # Get the draft session
        draft_stmt = select(DraftSession).where(
            DraftSession.session_id == quiz.draft_session_id
        )
        draft_result = await session.execute(draft_stmt)
        draft_session = draft_result.scalar_one_or_none()

        if not draft_session:
            print(f"  ⚠ Quiz {quiz.quiz_id}: Draft session {quiz.draft_session_id} not found")
            return False

        # Load draft analysis
        analysis = await DraftAnalysis.from_session(draft_session)
        if not analysis:
            print(f"  ⚠ Quiz {quiz.quiz_id}: Could not load DraftAnalysis")
            return False

        # Find the player's seat by matching name
        players = analysis.get_players()
        found_seat = None

        for player in players:
            if player.user_name == first_player_name:
                found_seat = player.seat_num
                break

        if found_seat is None:
            print(f"  ⚠ Quiz {quiz.quiz_id}: Could not find seat for player '{first_player_name}'")
            return False

        # Update the quiz with the seat
        await session.execute(
            update(QuizSession)
            .where(QuizSession.quiz_id == quiz.quiz_id)
            .values(starting_seat=found_seat)
        )

        print(f"  ✓ Quiz {quiz.quiz_id} (#{quiz.display_id}): seat={found_seat} (player: {first_player_name})")
        return True

    except Exception as e:
        print(f"  ✗ Quiz {quiz.quiz_id}: Error - {e}")
        return False


async def backfill_all():
    """Main backfill function."""
    print("🔧 Starting quiz seat backfill...")

    async with AsyncSessionLocal() as session:
        # Get all quizzes without starting_seat
        stmt = select(QuizSession).where(QuizSession.starting_seat.is_(None))
        result = await session.execute(stmt)
        quizzes = result.scalars().all()

        total = len(quizzes)
        if total == 0:
            print("✅ No quizzes need backfilling - all have starting_seat set!")
            return

        print(f"📊 Found {total} quizzes to process")

        success_count = 0
        fail_count = 0

        for i, quiz in enumerate(quizzes, 1):
            success = await backfill_quiz_seat(quiz, session)
            if success:
                success_count += 1
            else:
                fail_count += 1

            # Commit every 10 quizzes
            if i % 10 == 0:
                await session.commit()
                print(f"  📝 Committed batch ({i}/{total})...")

        await session.commit()

        print(f"\n✅ Backfill complete!")
        print(f"   - {success_count} quizzes updated successfully")
        print(f"   - {fail_count} quizzes failed")


if __name__ == "__main__":
    asyncio.run(backfill_all())
