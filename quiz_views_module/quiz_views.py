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
            # Show their previous results
            await interaction.response.send_message(
                f"You already submitted guesses for this quiz!\n\n"
                f"Your score: **{existing_submission.points_earned} points** ({existing_submission.correct_count}/4 correct)",
                ephemeral=True
            )
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

        # Create 4 dropdowns, one for each pick
        for i in range(4):
            pick = pack_trace.picks[i]
            select = QuizCardSelect(
                pick_number=i,
                pick_user_name=pick.user_name,
                card_options=card_options,
                row=i,
                parent_view=self
            )
            self.add_item(select)

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

        # Check that all 4 picks have been selected using stored selections
        guesses = []
        for i in range(4):
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

        correct_answers = quiz_session.correct_answers

        # Calculate results with point system (using module-level constants)
        correct_count = 0
        pick_results = []  # Boolean for exact matches
        pick_points = []   # Points for each pick

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
        if correct_count == 4:
            total_points += PERFECT_BONUS

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
        await self.show_results(interaction, guesses, correct_answers, pick_results, pick_points, total_points, stats)

    async def show_results(self, interaction, guesses, correct_answers, pick_results, pick_points, total_points, stats):
        """Display results to user"""
        embed = discord.Embed(
            title=f"Quiz Results: {total_points} Points!",
            description=f"**{sum(pick_results)}/4** exact matches",
            color=discord.Color.gold() if sum(pick_results) == 4 else discord.Color.green() if total_points >= 10 else discord.Color.orange()
        )

        # Show each pick result with points (using module-level constants)
        results_text = ""
        for i, (guess_id, correct_id, is_correct, points) in enumerate(zip(guesses, correct_answers, pick_results, pick_points)):
            pick = self.pack_trace.picks[i]
            guess_name = self.analysis.get_card(guess_id).name
            correct_name = self.analysis.get_card(correct_id).name

            if is_correct:
                icon = "✅"
                result_text = f"You guessed **{guess_name}** — {pick.user_name} picked: **{correct_name}** **+{points}pts**"
            elif points == TEAM_BONUS:
                icon = ":twisted_rightwards_arrows:"
                result_text = f"You guessed **{guess_name}** — {pick.user_name} picked: **{correct_name}** **+{TEAM_BONUS}pt** (right team, wrong seat)"
            else:
                icon = "❌"
                result_text = f"You guessed **{guess_name}** — {pick.user_name} picked: **{correct_name}**"

            results_text += f"{icon} **Pick {i+1}**: {result_text}\n"

        # Add perfect bonus note if applicable
        if sum(pick_results) == 4:
            results_text += f"\n🌟 **Perfect Score Bonus: +{PERFECT_BONUS}pts**"

        embed.add_field(name="Your Guesses", value=results_text, inline=False)

        # Show updated stats with points
        embed.add_field(
            name="Your Overall Stats",
            value=f"**Total Points:** {stats.total_points}\n"
                  f"**Average Points:** {stats.average_points_per_quiz:.1f} per quiz\n"
                  f"**Best Score:** {stats.highest_quiz_score} points\n"
                  f"**Accuracy:** {stats.accuracy_percentage:.1f}% (exact matches)\n"
                  f"**Perfect Streak:** {stats.current_perfect_streak} (longest: {stats.longest_perfect_streak})",
            inline=False
        )

        # Generate shareable text with emoji indicators
        emoji_line = ""
        for is_correct, points in zip(pick_results, pick_points):
            if is_correct:
                emoji_line += "✅"  # Exact match
            elif points == TEAM_BONUS:
                emoji_line += "🔀"  # Team bonus (twisted rightwards arrows)
            else:
                emoji_line += "❌"  # Incorrect

        share_text = f"🎯 Draft Pick Quiz\n{emoji_line} {total_points} pts"

        # Add share field with code block for easy copying
        embed.add_field(
            name="📤 Share Your Result",
            value=f"```\n{share_text}\n```\n",
            inline=False
        )

        await interaction.followup.send(embed=embed, ephemeral=True)
