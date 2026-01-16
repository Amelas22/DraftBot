#!/usr/bin/env python3
"""
Script to delete a quiz submission and update player stats.

Usage:
    pipenv run python scripts/delete_quiz_submission.py --quiz 58 --player "BroccoliRob" --dry-run
    pipenv run python scripts/delete_quiz_submission.py --quiz 58 --player "BroccoliRob"
"""

import argparse
import asyncio
import sys
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Add parent directory to path to import project modules
sys.path.append(str(Path(__file__).parent.parent))

from session import AsyncSessionLocal
from models.quiz_session import QuizSession
from models.quiz_submission import QuizSubmission
from models.quiz_stats import QuizStats
from loguru import logger


async def find_submission(quiz_display_id: str, player_name: str, session: AsyncSession):
    """Find a quiz submission by display_id and player name."""
    stmt = (
        select(QuizSubmission, QuizSession)
        .join(QuizSession, QuizSubmission.quiz_id == QuizSession.quiz_id)
        .where(QuizSession.display_id == quiz_display_id)
        .where(QuizSubmission.display_name.like(f"%{player_name}%"))
    )
    result = await session.execute(stmt)
    row = result.first()

    if not row:
        return None, None

    return row[0], row[1]  # submission, quiz_session


async def get_player_stats(player_id: str, guild_id: str, session: AsyncSession):
    """Get player's current quiz stats."""
    stmt = select(QuizStats).where(
        QuizStats.player_id == player_id,
        QuizStats.guild_id == guild_id
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def recalculate_stats(player_id: str, guild_id: str, session: AsyncSession):
    """Recalculate all stats from scratch by querying all submissions."""
    stmt = (
        select(QuizSubmission)
        .join(QuizSession, QuizSubmission.quiz_id == QuizSession.quiz_id)
        .where(
            QuizSubmission.player_id == player_id,
            QuizSession.guild_id == guild_id
        )
        .order_by(QuizSubmission.submitted_at)
    )
    result = await session.execute(stmt)
    submissions = result.scalars().all()

    if not submissions:
        return None

    # Calculate totals
    total_quizzes = len(submissions)
    total_picks_attempted = total_quizzes * 4
    total_picks_correct = sum(sub.correct_count for sub in submissions)
    total_points = sum(sub.points_earned for sub in submissions)

    # Calculate percentages
    accuracy_percentage = (total_picks_correct / total_picks_attempted * 100) if total_picks_attempted > 0 else 0.0
    average_points_per_quiz = (total_points / total_quizzes) if total_quizzes > 0 else 0.0

    # Find highest score
    highest_quiz_score = max(sub.points_earned for sub in submissions)

    # Calculate perfect streak
    current_perfect_streak = 0
    longest_perfect_streak = 0
    current_streak = 0

    for sub in reversed(submissions):  # Most recent first for current streak
        if sub.correct_count == 4:
            current_streak += 1
        else:
            if current_streak > longest_perfect_streak:
                longest_perfect_streak = current_streak
            if current_perfect_streak == 0:  # First non-perfect found
                current_perfect_streak = current_streak
            current_streak = 0

    # Check if streak extends to end
    if current_streak > longest_perfect_streak:
        longest_perfect_streak = current_streak
    if current_perfect_streak == 0:
        current_perfect_streak = current_streak

    # Get last submission time
    last_quiz_time = submissions[-1].submitted_at

    return {
        'total_quizzes': total_quizzes,
        'total_picks_attempted': total_picks_attempted,
        'total_picks_correct': total_picks_correct,
        'accuracy_percentage': accuracy_percentage,
        'total_points': total_points,
        'average_points_per_quiz': average_points_per_quiz,
        'highest_quiz_score': highest_quiz_score,
        'current_perfect_streak': current_perfect_streak,
        'longest_perfect_streak': longest_perfect_streak,
        'last_quiz_time': last_quiz_time
    }


async def delete_submission(quiz_display_id: str, player_name: str, dry_run: bool = True):
    """Delete a quiz submission and update stats."""
    async with AsyncSessionLocal() as session:
        # Find the submission
        submission, quiz_session = await find_submission(quiz_display_id, player_name, session)

        if not submission:
            logger.error(f"No submission found for quiz #{quiz_display_id} by player '{player_name}'")
            return False

        # Get current stats
        stats = await get_player_stats(submission.player_id, quiz_session.guild_id, session)

        logger.info(f"\n{'='*80}")
        logger.info(f"Found submission for Quiz #{quiz_display_id}")
        logger.info(f"Player: {submission.display_name} (ID: {submission.player_id})")
        logger.info(f"Submitted: {submission.submitted_at}")
        logger.info(f"Score: {submission.points_earned} points ({submission.correct_count}/4 correct)")
        logger.info(f"Pick results: {submission.pick_1_correct}, {submission.pick_2_correct}, {submission.pick_3_correct}, {submission.pick_4_correct}")
        logger.info(f"Pick points: {submission.pick_1_points}, {submission.pick_2_points}, {submission.pick_3_points}, {submission.pick_4_points}")
        logger.info(f"{'='*80}\n")

        if stats:
            logger.info("Current Stats:")
            logger.info(f"  Total quizzes: {stats.total_quizzes}")
            logger.info(f"  Total picks: {stats.total_picks_correct}/{stats.total_picks_attempted} ({stats.accuracy_percentage:.2f}%)")
            logger.info(f"  Total points: {stats.total_points} (avg: {stats.average_points_per_quiz:.2f})")
            logger.info(f"  Highest score: {stats.highest_quiz_score}")
            logger.info(f"  Perfect streak: {stats.current_perfect_streak} (longest: {stats.longest_perfect_streak})")
            logger.info(f"  Last quiz: {stats.last_quiz_time}\n")

        if dry_run:
            logger.warning("DRY RUN - No changes will be made")
            logger.info("\nWould delete submission and recalculate stats from remaining submissions")

            # Store current stats values before any session changes
            old_total_quizzes = stats.total_quizzes if stats else 0
            old_accuracy = stats.accuracy_percentage if stats else 0.0
            old_avg_points = stats.average_points_per_quiz if stats else 0.0

            # Show what stats would be after deletion
            await session.delete(submission)
            new_stats = await recalculate_stats(submission.player_id, quiz_session.guild_id, session)
            await session.rollback()  # Undo the delete

            if new_stats:
                logger.info("\nProjected Stats After Deletion:")
                logger.info(f"  Total quizzes: {new_stats['total_quizzes']}")
                logger.info(f"  Total picks: {new_stats['total_picks_correct']}/{new_stats['total_picks_attempted']} ({new_stats['accuracy_percentage']:.2f}%)")
                logger.info(f"  Total points: {new_stats['total_points']} (avg: {new_stats['average_points_per_quiz']:.2f})")
                logger.info(f"  Highest score: {new_stats['highest_quiz_score']}")
                logger.info(f"  Perfect streak: {new_stats['current_perfect_streak']} (longest: {new_stats['longest_perfect_streak']})")
                logger.info(f"  Last quiz: {new_stats['last_quiz_time']}")

                logger.info("\nChanges:")
                if stats:
                    logger.info(f"  Quizzes: {old_total_quizzes} → {new_stats['total_quizzes']} ({new_stats['total_quizzes'] - old_total_quizzes:+d})")
                    logger.info(f"  Accuracy: {old_accuracy:.2f}% → {new_stats['accuracy_percentage']:.2f}% ({new_stats['accuracy_percentage'] - old_accuracy:+.2f}%)")
                    logger.info(f"  Avg points: {old_avg_points:.2f} → {new_stats['average_points_per_quiz']:.2f} ({new_stats['average_points_per_quiz'] - old_avg_points:+.2f})")
            else:
                logger.info("\nNo submissions would remain after deletion - stats would be deleted")

            logger.warning("\nTo apply changes, run without --dry-run flag")
            return False

        # Actually delete and update
        async with session.begin():
            # Delete the submission
            await session.delete(submission)
            await session.flush()

            # Recalculate stats from remaining submissions
            new_stats = await recalculate_stats(submission.player_id, quiz_session.guild_id, session)

            if new_stats and stats:
                # Update existing stats record
                stats.total_quizzes = new_stats['total_quizzes']
                stats.total_picks_attempted = new_stats['total_picks_attempted']
                stats.total_picks_correct = new_stats['total_picks_correct']
                stats.accuracy_percentage = new_stats['accuracy_percentage']
                stats.total_points = new_stats['total_points']
                stats.average_points_per_quiz = new_stats['average_points_per_quiz']
                stats.highest_quiz_score = new_stats['highest_quiz_score']
                stats.current_perfect_streak = new_stats['current_perfect_streak']
                stats.longest_perfect_streak = new_stats['longest_perfect_streak']
                stats.last_quiz_time = new_stats['last_quiz_time']

                logger.success(f"✓ Deleted submission for quiz #{quiz_display_id}")
                logger.success(f"✓ Updated stats for {submission.display_name}")
                logger.info(f"\nNew stats:")
                logger.info(f"  Total quizzes: {stats.total_quizzes}")
                logger.info(f"  Accuracy: {stats.accuracy_percentage:.2f}%")
                logger.info(f"  Avg points: {stats.average_points_per_quiz:.2f}")
            elif not new_stats and stats:
                # No more submissions - delete stats record
                await session.delete(stats)
                logger.success(f"✓ Deleted submission for quiz #{quiz_display_id}")
                logger.success(f"✓ Deleted stats record (no remaining submissions)")
            else:
                logger.success(f"✓ Deleted submission for quiz #{quiz_display_id}")

        return True


def main():
    parser = argparse.ArgumentParser(
        description='Delete a quiz submission and update player stats',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run to see what would happen
  pipenv run python scripts/delete_quiz_submission.py --quiz 58 --player "BroccoliRob" --dry-run

  # Actually delete the submission
  pipenv run python scripts/delete_quiz_submission.py --quiz 58 --player "BroccoliRob"

  # Partial name match works
  pipenv run python scripts/delete_quiz_submission.py --quiz 58 --player "Broccoli" --dry-run
        """
    )
    parser.add_argument('--quiz', required=True, help='Quiz display ID (e.g., 58)')
    parser.add_argument('--player', required=True, help='Player display name (partial match supported)')
    parser.add_argument('--dry-run', action='store_true', help='Show what would happen without making changes')

    args = parser.parse_args()

    # Run the async function
    success = asyncio.run(delete_submission(args.quiz, args.player, args.dry_run))

    if not success and not args.dry_run:
        exit(1)


if __name__ == '__main__':
    main()
