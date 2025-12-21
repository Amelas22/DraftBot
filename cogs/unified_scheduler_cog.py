import discord
from discord.ext import commands, tasks
from datetime import datetime
from loguru import logger
from sqlalchemy import select
import pytz
from database.db_session import db_session
from models.draft_logs import LogChannel, PostSchedule
from models.quiz_scheduling import QuizChannel, QuizSchedule


class UnifiedSchedulerCog(commands.Cog):
    """
    Unified scheduler that checks both draft log and quiz schedules.
    Scheduling logic moved here from draft_logs_cog.py for centralized management.
    """

    def __init__(self, bot):
        self.bot = bot
        logger.info("Unified Scheduler cog initialized")
        self.check_all_schedules.start()

    def cog_unload(self):
        self.check_all_schedules.cancel()

    @tasks.loop(minutes=1)
    async def check_all_schedules(self):
        """Check schedules for both draft logs and quizzes every minute"""
        try:
            await self.bot.wait_until_ready()

            # Check draft log schedules
            await self.check_draft_log_schedules()

            # Check quiz schedules
            await self.check_quiz_schedules()

        except Exception as e:
            logger.error(f"Error in check_all_schedules task: {e}", exc_info=True)

    async def check_draft_log_schedules(self):
        """Check if it's time to post a draft log in any channels"""
        try:
            async with db_session() as session:
                # Get all log channels and their schedules
                stmt = select(LogChannel, PostSchedule).join(
                    PostSchedule, LogChannel.channel_id == PostSchedule.channel_id
                )
                result = await session.execute(stmt)
                channel_schedules = result.all()

                for log_channel, schedule in channel_schedules:
                    try:
                        # Skip disabled channels
                        if not log_channel.enabled:
                            continue

                        # Get current time in the channel's time zone
                        tz = pytz.timezone(log_channel.time_zone)
                        current_time = datetime.now(tz).strftime("%H:%M")

                        # Check if it's time to post
                        if current_time == schedule.post_time:
                            logger.info(f"Posting draft log in channel {log_channel.channel_id} at {current_time} {log_channel.time_zone}")

                            # Get the draft logs cog and call its post method
                            draft_logs_cog = self.bot.get_cog("DraftLogsCog")
                            if draft_logs_cog:
                                await draft_logs_cog.post_draft_log(log_channel.channel_id)
                            else:
                                logger.error("DraftLogsCog not found")

                    except Exception as e:
                        logger.error(f"Error posting scheduled draft log to channel {log_channel.channel_id}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error in check_draft_log_schedules: {e}", exc_info=True)

    async def check_quiz_schedules(self):
        """Check if it's time to post a quiz in any channels"""
        try:
            async with db_session() as session:
                # Get all quiz channels and their schedules
                stmt = select(QuizChannel, QuizSchedule).join(
                    QuizSchedule, QuizChannel.channel_id == QuizSchedule.channel_id
                )
                result = await session.execute(stmt)
                channel_schedules = result.all()

                for quiz_channel, schedule in channel_schedules:
                    try:
                        # Skip disabled channels
                        if not quiz_channel.enabled:
                            continue

                        # Get current time in the channel's time zone
                        tz = pytz.timezone(quiz_channel.time_zone)
                        current_time = datetime.now(tz).strftime("%H:%M")

                        # Check if it's time to post
                        if current_time == schedule.post_time:
                            logger.info(f"Posting quiz in channel {quiz_channel.channel_id} at {current_time} {quiz_channel.time_zone}")

                            # Get the quiz commands cog and call its post method
                            quiz_commands_cog = self.bot.get_cog("QuizCommands")
                            if quiz_commands_cog:
                                success = await quiz_commands_cog.post_scheduled_quiz(quiz_channel.channel_id)
                                if success:
                                    # Update last_post timestamp
                                    quiz_channel.last_post = datetime.now()
                                    session.add(quiz_channel)
                                    await session.commit()
                                else:
                                    logger.warning(f"Failed to post quiz to channel {quiz_channel.channel_id}")
                            else:
                                logger.error("QuizCommands cog not found")

                    except Exception as e:
                        logger.error(f"Error posting scheduled quiz to channel {quiz_channel.channel_id}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error in check_quiz_schedules: {e}", exc_info=True)


def setup(bot):
    bot.add_cog(UnifiedSchedulerCog(bot))
