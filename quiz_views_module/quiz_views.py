import discord
from datetime import datetime
from loguru import logger
from sqlalchemy import select, update, and_
from database.db_session import db_session
from models import QuizSession, QuizSubmission, QuizStats, DraftSession
from services.draft_analysis import DraftAnalysis
from models.draft_domain import PackTrace


# Quiz scoring constants
PICK_WEIGHTS = [2, 3, 4, 5]  # Points for exact matches on picks 1-4
PERFECT_BONUS = 5  # Bonus points for getting all 4 picks exactly correct
TEAM_BONUS = 1  # Points for correct card picked by the right team, wrong seat

# Emoji constants
EMOJI_CORRECT = "✅"
EMOJI_TEAM_BONUS = ":twisted_rightwards_arrows:"
EMOJI_INCORRECT = "❌"
NUM_PICKS = 4  # Number of picks in the quiz


# Helper functions
def _get_result_color(correct_count: int, total_points: int) -> discord.Color:
    """Determine embed color based on performance."""
    if correct_count == NUM_PICKS:
        return discord.Color.gold()
    elif total_points >= 10:
        return discord.Color.green()
    else:
        return discord.Color.orange()


def _format_stats_field(stats: QuizStats) -> str:
    """Format player stats for display."""
    return (
        f"**Total Points:** {stats.total_points}\n"
        f"**Average Points:** {stats.average_points_per_quiz:.1f} per quiz\n"
        f"**Best Score:** {stats.highest_quiz_score} points\n"
        f"**Accuracy:** {stats.accuracy_percentage:.1f}% (exact matches)\n"
        f"**Perfect Streak:** {stats.current_perfect_streak} (longest: {stats.longest_perfect_streak})"
    )


def _generate_result_emoji_line(pick_results: list[bool], pick_points: list[int]) -> str:
    """Generate emoji representation of quiz results."""
    return "".join(
        EMOJI_CORRECT if is_correct else EMOJI_TEAM_BONUS if points == TEAM_BONUS else EMOJI_INCORRECT
        for is_correct, points in zip(pick_results, pick_points)
    )


async def _display_results(
    interaction: discord.Interaction,
    analysis: DraftAnalysis,
    pack_trace: PackTrace,
    guesses: list,
    correct_answers: list,
    pick_results: list,
    pick_points: list,
    total_points: int,
    correct_count: int,
    stats: QuizStats = None,
    is_existing_submission: bool = False
) -> None:
    """
    Unified function to display quiz results.

    Args:
        interaction: Discord interaction
        analysis: DraftAnalysis instance for card lookups
        pack_trace: PackTrace instance for pick information
        guesses: User's guessed card IDs
        correct_answers: Correct card IDs
        pick_results: Boolean array of exact matches
        pick_points: Points earned for each pick
        total_points: Total points earned
        correct_count: Number of exact matches
        stats: Player's quiz stats (optional)
        is_existing_submission: True if showing previously submitted results
    """
    embed = discord.Embed(
        title=f"{'Your ' if is_existing_submission else ''}Quiz Results: {total_points} Points!",
        description=(
            f"**{correct_count}/{NUM_PICKS}** exact matches" +
            ("\n*(You submitted this quiz earlier)*" if is_existing_submission else "")
        ),
        color=_get_result_color(correct_count, total_points)
    )

    # Show each pick result with points
    results_text = ""
    for i, (guess_id, correct_id, is_correct, points) in enumerate(
        zip(guesses, correct_answers, pick_results, pick_points)
    ):
        pick = pack_trace.picks[i]
        guess_name = analysis.get_card(guess_id).name
        correct_name = analysis.get_card(correct_id).name

        if is_correct:
            icon = EMOJI_CORRECT
            result_text = f"You guessed **{guess_name}** — {pick.user_name} picked: **{correct_name}** **+{points}pts**"
        elif points == TEAM_BONUS:
            icon = EMOJI_TEAM_BONUS
            result_text = f"You guessed **{guess_name}** — {pick.user_name} picked: **{correct_name}** **+{TEAM_BONUS}pt** (right team, wrong seat)"
        else:
            icon = EMOJI_INCORRECT
            result_text = f"You guessed **{guess_name}** — {pick.user_name} picked: **{correct_name}**"

        results_text += f"{icon} **Pick {i+1}**: {result_text}\n"

    # Add perfect bonus note if applicable
    if correct_count == NUM_PICKS:
        results_text += f"\n🌟 **Perfect Score Bonus: +{PERFECT_BONUS}pts**"

    embed.add_field(name="Your Guesses", value=results_text, inline=False)

    # Show stats if available
    if stats:
        embed.add_field(
            name="Your Overall Stats",
            value=_format_stats_field(stats),
            inline=False
        )

    # Generate shareable text with emoji indicators
    emoji_line = _generate_result_emoji_line(pick_results, pick_points)
    share_text = f"🎯 Draft Pick Quiz\n{emoji_line} {total_points} pts"

    embed.add_field(
        name="📤 Share Your Result",
        value=f"```\n{share_text}\n```\n",
        inline=False
    )

    await interaction.followup.send(embed=embed, ephemeral=True)


class QuizCardSelect(discord.ui.Select):
    """Custom Select for card selection with interaction acknowledgment"""

    def __init__(self, pick_number: int, pick_user_name: str, card_options: list, row: int, parent_view):
        self.pick_number = pick_number
        self.parent_view = parent_view

        options = [
            discord.SelectOption(
                label=name[:100],  # Truncate if needed
                value=card_id
            )
            for card_id, name in card_options
        ]

        super().__init__(
            placeholder=f"Pick {pick_number + 1}: {pick_user_name}",
            options=options,
            row=row
        )
        # Discord auto-generates custom_id for ephemeral views

    async def callback(self, interaction: discord.Interaction):
        """Store the selection and acknowledge the interaction"""
        # Store the selected value in the parent view
        if self.values:
            self.parent_view.selections[self.pick_number] = self.values[0]
            logger.debug(f"User {interaction.user.id} selected card for pick {self.pick_number + 1}: {self.values[0]}")

        await interaction.response.defer()


class QuizPublicView(discord.ui.View):
    """
    Public view with persistent 'Make Your Guesses' button.
    Attached to the quiz message, survives bot restarts.
    Compatible with sticky message system.
    """

    def __init__(self, quiz_id: str, analysis: DraftAnalysis = None, pack_trace: PackTrace = None):
        super().__init__(timeout=None)  # Persistent across restarts
        self.quiz_id = quiz_id
        self.analysis = analysis
        self.pack_trace = pack_trace

    async def _load_quiz_data(self) -> bool:
        """
        Lazy load analysis and pack_trace from database.
        Returns True if successful, False otherwise.
        """
        if self.analysis is not None and self.pack_trace is not None:
            return True  # Already loaded

        async with db_session() as session:
            stmt = select(QuizSession).where(QuizSession.quiz_id == self.quiz_id)
            result = await session.execute(stmt)
            quiz_session = result.scalar_one_or_none()

            if not quiz_session:
                logger.error(f"QuizSession {self.quiz_id} not found")
                return False

            stmt = select(DraftSession).where(DraftSession.session_id == quiz_session.draft_session_id)
            result = await session.execute(stmt)
            draft_session = result.scalar_one_or_none()

        if not draft_session:
            logger.error(f"DraftSession not found for quiz {self.quiz_id}")
            return False

        try:
            self.analysis = await DraftAnalysis.from_session(draft_session)
            if self.analysis:
                self.pack_trace = self.analysis.trace_pack(pack_num=0, length=4)
                return self.pack_trace is not None
        except Exception as e:
            logger.error(f"Error loading quiz data for {self.quiz_id}: {e}", exc_info=True)

        return False

    def to_metadata(self) -> dict:
        """Convert view properties to a dictionary for JSON storage (sticky message system)."""
        return {
            "quiz_id": self.quiz_id,
            "view_type": "quiz"
        }

    @classmethod
    async def from_metadata(cls, bot, metadata: dict):
        """
        Recreate QuizPublicView from stored metadata (sticky message system).
        Reloads quiz data from database and regenerates analysis/pack_trace.
        """
        quiz_id = metadata.get("quiz_id")
        view = cls(quiz_id=quiz_id)
        await view._load_quiz_data()  # Load data (may fail silently)
        return view

    async def _load_quiz_session(self, quiz_id: str) -> QuizSession | None:
        """Load quiz session from database."""
        async with db_session() as session:
            stmt = select(QuizSession).where(QuizSession.quiz_id == quiz_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def _load_player_stats(self, player_id: str, guild_id: str) -> QuizStats | None:
        """Load player stats from database."""
        async with db_session() as session:
            stmt = select(QuizStats).where(
                and_(
                    QuizStats.player_id == player_id,
                    QuizStats.guild_id == guild_id
                )
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def _show_existing_results(self, interaction: discord.Interaction, submission: QuizSubmission):
        """
        Show full results panel from a previous submission.

        Args:
            interaction: Discord interaction
            submission: Existing QuizSubmission record
        """
        await interaction.response.defer(ephemeral=True)

        # Load quiz session and stats
        quiz_session = await self._load_quiz_session(self.quiz_id)
        if not quiz_session:
            await interaction.followup.send("Quiz not found!", ephemeral=True)
            return

        stats = await self._load_player_stats(str(interaction.user.id), quiz_session.guild_id)

        # Use helper properties to get arrays from submission
        await _display_results(
            interaction,
            self.analysis,
            self.pack_trace,
            guesses=submission.guesses,
            correct_answers=quiz_session.correct_answers,
            pick_results=submission.pick_results_array,
            pick_points=submission.pick_points_array,
            total_points=submission.points_earned,
            correct_count=submission.correct_count,
            stats=stats,
            is_existing_submission=True
        )

    @discord.ui.button(
        label="Make Your Guesses",
        style=discord.ButtonStyle.primary,
        custom_id="quiz_make_guesses"
    )
    async def make_guesses_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """User clicks to participate - show ephemeral guess view"""
        user_id = str(interaction.user.id)

        # Lazy load quiz data if not available (happens after sticky message repost)
        if not await self._load_quiz_data():
            await interaction.response.send_message(
                "Error: Could not load quiz data. Please try again later.",
                ephemeral=True
            )
            return

        # Check if user already submitted
        async with db_session() as session:
            stmt = select(QuizSubmission).where(
                and_(
                    QuizSubmission.quiz_id == self.quiz_id,
                    QuizSubmission.player_id == user_id
                )
            )
            result = await session.execute(stmt)
            existing_submission = result.scalar_one_or_none()

        if existing_submission:
            # Show their full results panel from when they submitted
            await self._show_existing_results(interaction, existing_submission)
            return

        # Show ephemeral guess view
        guess_view = QuizGuessView(
            quiz_id=self.quiz_id,
            analysis=self.analysis,
            pack_trace=self.pack_trace,
            user=interaction.user
        )

        await interaction.response.send_message(
            "Select your guesses for each pick:",
            view=guess_view,
            ephemeral=True
        )


class QuizGuessView(discord.ui.View):
    """
    Ephemeral view shown to individual users.
    Contains 4 dropdowns (one per pick) and a Submit button.
    """

    def __init__(self, quiz_id: str, analysis: DraftAnalysis, pack_trace: PackTrace, user: discord.User):
        super().__init__(timeout=300)  # 5 minute timeout
        self.quiz_id = quiz_id
        self.analysis = analysis
        self.pack_trace = pack_trace
        self.user = user
        self.selections = {}  # Store user selections {pick_number: card_id}
        logger.debug(f"Created new QuizGuessView for user {user.id} (quiz {quiz_id})")

        # Get card options (all 15 cards from first pick)
        first_pick = pack_trace.picks[0]
        card_options = sorted([
            (cid, analysis.get_card(cid).name)
            for cid in first_pick.booster_ids
        ], key=lambda x: x[1])  # Sort by name

        # Create dropdowns, one for each pick
        for i in range(NUM_PICKS):
            pick = pack_trace.picks[i]
            select = QuizCardSelect(
                pick_number=i,
                pick_user_name=pick.user_name,
                card_options=card_options,
                row=i,
                parent_view=self
            )
            self.add_item(select)

    def _calculate_results(
        self,
        guesses: list[str],
        correct_answers: list[str]
    ) -> tuple[list[bool], list[int], int, int]:
        """
        Calculate quiz results with scoring.

        Args:
            guesses: User's guessed card IDs
            correct_answers: Correct card IDs

        Returns:
            tuple: (pick_results, pick_points, total_points, correct_count)
        """
        correct_count = 0
        pick_results = []
        pick_points = []

        for i, (guess_id, correct_id) in enumerate(zip(guesses, correct_answers)):
            pick_position = i + 1  # 1-indexed

            if guess_id == correct_id:
                # Exact match - full points
                is_correct = True
                points = PICK_WEIGHTS[i]
                correct_count += 1
            elif guess_id in correct_answers:
                # Correct card, wrong position - check parity
                is_correct = False
                correct_position = correct_answers.index(guess_id) + 1  # 1-indexed

                # Check if parity matches (both odd or both even) = same team
                if (pick_position % 2) == (correct_position % 2):
                    points = TEAM_BONUS
                else:
                    points = 0  # No points
            else:
                # Wrong card entirely
                is_correct = False
                points = 0

            pick_results.append(is_correct)
            pick_points.append(points)

        # Calculate total points with perfect bonus
        total_points = sum(pick_points)
        if correct_count == NUM_PICKS:
            total_points += PERFECT_BONUS

        return pick_results, pick_points, total_points, correct_count

    @discord.ui.button(
        label="Submit Guesses",
        style=discord.ButtonStyle.success,
        row=4  # Place in last row (after 4 dropdowns)
        # Note: custom_id for buttons in ephemeral views doesn't need to be unique per user
    )
    async def submit_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Process submission and show results"""
        await interaction.response.defer(ephemeral=True)

        # Check if view has any selections at all (detects stale/restarted views)
        if not self.selections:
            logger.warning(f"User {interaction.user.id} submitted with no selections stored (possible bot restart or timeout)")
            await interaction.followup.send(
                "❌ Your quiz session has expired or was lost (possibly due to bot restart).\n\n"
                "Please click **Make Your Guesses** again to start a fresh quiz!",
                ephemeral=True
            )
            return

        # Check that all picks have been selected using stored selections
        guesses = []
        for i in range(NUM_PICKS):
            if i not in self.selections:
                logger.warning(f"User {interaction.user.id} missing selection for pick {i+1} (has {len(self.selections)} selections)")
                await interaction.followup.send(
                    f"Please select a card for Pick {i+1}!",
                    ephemeral=True
                )
                return
            guesses.append(self.selections[i])

        logger.info(f"User {interaction.user.id} submitting guesses: {guesses}")

        # Load correct answers from database
        async with db_session() as session:
            stmt = select(QuizSession).where(QuizSession.quiz_id == self.quiz_id)
            result = await session.execute(stmt)
            quiz_session = result.scalar_one_or_none()

        if not quiz_session:
            await interaction.followup.send("Quiz not found!", ephemeral=True)
            return

        # Calculate results using helper method
        pick_results, pick_points, total_points, correct_count = self._calculate_results(
            guesses, quiz_session.correct_answers
        )

        # Save submission
        async with db_session() as session:
            submission = QuizSubmission(
                quiz_id=self.quiz_id,
                player_id=str(self.user.id),
                display_name=self.user.display_name,
                guesses=guesses,
                correct_count=correct_count,
                pick_1_correct=pick_results[0],
                pick_2_correct=pick_results[1],
                pick_3_correct=pick_results[2],
                pick_4_correct=pick_results[3],
                points_earned=total_points,
                pick_1_points=pick_points[0],
                pick_2_points=pick_points[1],
                pick_3_points=pick_points[2],
                pick_4_points=pick_points[3]
            )
            session.add(submission)

            # Update or create QuizStats
            stmt = select(QuizStats).where(
                and_(
                    QuizStats.player_id == str(self.user.id),
                    QuizStats.guild_id == quiz_session.guild_id
                )
            )
            result = await session.execute(stmt)
            stats = result.scalar_one_or_none()

            if not stats:
                stats = QuizStats(
                    player_id=str(self.user.id),
                    guild_id=quiz_session.guild_id,
                    display_name=self.user.display_name
                )
                session.add(stats)

            stats.update_stats(correct_count, total_points)
            stats.last_quiz_time = datetime.now()

            # Update quiz session participant count
            await session.execute(
                update(QuizSession)
                .where(QuizSession.quiz_id == self.quiz_id)
                .values(total_participants=QuizSession.total_participants + 1)
            )

            await session.commit()

        # Show results
        await self.show_results(interaction, guesses, quiz_session.correct_answers, pick_results, pick_points, total_points, correct_count, stats)

    async def show_results(self, interaction, guesses, correct_answers, pick_results, pick_points, total_points, correct_count, stats):
        """Display results to user"""
        await _display_results(
            interaction,
            self.analysis,
            self.pack_trace,
            guesses=guesses,
            correct_answers=correct_answers,
            pick_results=pick_results,
            pick_points=pick_points,
            total_points=total_points,
            correct_count=correct_count,
            stats=stats,
            is_existing_submission=False
        )
