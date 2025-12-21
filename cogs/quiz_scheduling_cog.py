import discord
from discord.ext import commands
import asyncio
from datetime import datetime
from loguru import logger
from sqlalchemy import select, and_, or_, desc, func
import pytz
from typing import Optional, List
from database.db_session import db_session
from models.quiz_scheduling import QuizChannel, QuizSchedule

class QuizSchedulingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        logger.info("Quiz Scheduling cog initialized")

    @discord.slash_command(
        name="setup_quiz_channel",
        description="Set up a channel for posting scheduled quizzes"
    )
    @commands.has_permissions(manage_roles=True)
    async def setup_quiz_channel(
        self,
        ctx,
        channel: discord.TextChannel,
        time_zone: Optional[str] = "UTC"
    ):
        """
        Set up a channel for posting scheduled quizzes

        Parameters
        ----------
        channel: The channel to post quizzes in
        time_zone: Time zone (default: UTC)
        """
        await ctx.defer(ephemeral=True)

        # Validate the time zone
        try:
            tz = pytz.timezone(time_zone)
        except Exception:
            await ctx.followup.send(
                f"Unknown time zone: {time_zone}. Please use a valid time zone name (e.g., 'US/Eastern', 'Europe/London').",
                ephemeral=True
            )
            return

        async with db_session() as session:
            # Check if channel is already set up
            stmt = select(QuizChannel).where(QuizChannel.channel_id == str(channel.id))
            result = await session.execute(stmt)
            existing_channel = result.scalar_one_or_none()

            if existing_channel:
                await ctx.followup.send(
                    f"Channel {channel.mention} is already set up for quizzes. Use `/edit_quiz_timezone` to modify settings.",
                    ephemeral=True
                )
                return

            # Create new quiz channel entry
            new_channel = QuizChannel(
                channel_id=str(channel.id),
                guild_id=str(ctx.guild.id),
                time_zone=time_zone
            )

            session.add(new_channel)
            await session.commit()

        await ctx.followup.send(
            f"✅ Successfully set up {channel.mention} for scheduled quizzes with time zone {time_zone}. "
            f"Now add posting schedules with `/add_quiz_schedule`.",
            ephemeral=True
        )

    @discord.slash_command(
        name="add_quiz_schedule",
        description="Add a posting schedule for quizzes"
    )
    @commands.has_permissions(manage_roles=True)
    async def add_quiz_schedule(
        self,
        ctx,
        channel: discord.TextChannel,
        hour: int,
        minute: int
    ):
        """
        Add a posting schedule for quizzes

        Parameters
        ----------
        channel: The channel to add a schedule for
        hour: Hour of day (0-23)
        minute: Minute of hour (0-59)
        """
        await ctx.defer(ephemeral=True)

        # Validate time inputs
        if not (0 <= hour <= 23):
            await ctx.followup.send("Hour must be between 0 and 23.", ephemeral=True)
            return

        if not (0 <= minute <= 59):
            await ctx.followup.send("Minute must be between 0 and 59.", ephemeral=True)
            return

        # Format time as HH:MM
        post_time = f"{hour:02d}:{minute:02d}"

        async with db_session() as session:
            # Check if channel is set up
            stmt = select(QuizChannel).where(QuizChannel.channel_id == str(channel.id))
            result = await session.execute(stmt)
            quiz_channel = result.scalar_one_or_none()

            if not quiz_channel:
                await ctx.followup.send(
                    f"Channel {channel.mention} is not set up for quizzes. Use `/setup_quiz_channel` first.",
                    ephemeral=True
                )
                return

            # Check if this schedule already exists
            stmt = select(QuizSchedule).where(
                and_(
                    QuizSchedule.channel_id == str(channel.id),
                    QuizSchedule.post_time == post_time
                )
            )
            result = await session.execute(stmt)
            existing_schedule = result.scalar_one_or_none()

            if existing_schedule:
                await ctx.followup.send(
                    f"A schedule for {post_time} already exists in {channel.mention}.",
                    ephemeral=True
                )
                return

            # Create new schedule
            new_schedule = QuizSchedule(
                channel_id=str(channel.id),
                post_time=post_time
            )

            session.add(new_schedule)
            await session.commit()

        await ctx.followup.send(
            f"✅ Successfully added quiz posting schedule at {post_time} ({quiz_channel.time_zone}) for {channel.mention}",
            ephemeral=True
        )

    @discord.slash_command(
        name="list_quiz_schedules",
        description="List all posting schedules for quizzes"
    )
    @commands.has_permissions(manage_roles=True)
    async def list_quiz_schedules(
        self,
        ctx,
        channel: discord.TextChannel
    ):
        """
        List all posting schedules for quizzes

        Parameters
        ----------
        channel: The channel to list schedules for
        """
        await ctx.defer(ephemeral=True)

        # Get the QuizChannel to check enabled status
        async with db_session() as session:
            stmt = select(QuizChannel).where(QuizChannel.channel_id == str(channel.id))
            result = await session.execute(stmt)
            quiz_channel = result.scalar_one_or_none()

            if not quiz_channel:
                await ctx.followup.send(
                    f"❌ {channel.mention} is not set up for quizzes. Use `/setup_quiz_channel` first.",
                    ephemeral=True
                )
                return

            enabled_status = "✅ Enabled" if quiz_channel.enabled else "🔴 Disabled"

        schedules = await self.get_channel_schedules(channel.id)

        if not schedules:
            await ctx.followup.send(
                f"**Status:** {enabled_status}\n\nNo schedules found for {channel.mention}. Add one with `/add_quiz_schedule`.",
                ephemeral=True
            )
            return

        schedule_list = "\n".join([f"• ID: {schedule[0]} - Time: {schedule[1]}" for schedule in schedules])

        await ctx.followup.send(
            f"**Status:** {enabled_status}\n\nPosting schedules for {channel.mention}:\n{schedule_list}",
            ephemeral=True
        )

    @discord.slash_command(
        name="remove_quiz_schedule",
        description="Remove a quiz posting schedule"
    )
    @commands.has_permissions(manage_roles=True)
    async def remove_quiz_schedule(
        self,
        ctx,
        schedule_id: int
    ):
        """
        Remove a quiz posting schedule

        Parameters
        ----------
        schedule_id: The ID of the schedule to remove (from /list_quiz_schedules)
        """
        await ctx.defer(ephemeral=True)

        async with db_session() as session:
            # Get the schedule
            stmt = select(QuizSchedule).where(QuizSchedule.id == schedule_id)
            result = await session.execute(stmt)
            schedule = result.scalar_one_or_none()

            if not schedule:
                await ctx.followup.send(f"Schedule ID {schedule_id} not found.", ephemeral=True)
                return

            channel_id = schedule.channel_id
            post_time = schedule.post_time

            # Delete the schedule
            await session.delete(schedule)
            await session.commit()

        # Get channel mention for response
        channel = self.bot.get_channel(int(channel_id))
        if channel:
            channel_mention = channel.mention
        else:
            channel_mention = f"channel {channel_id}"

        await ctx.followup.send(
            f"✅ Removed quiz posting schedule at {post_time} for {channel_mention}.",
            ephemeral=True
        )

    @discord.slash_command(
        name="edit_quiz_timezone",
        description="Edit the timezone for a quiz channel"
    )
    @commands.has_permissions(manage_roles=True)
    async def edit_quiz_timezone(
        self,
        ctx,
        channel: discord.TextChannel,
        time_zone: str
    ):
        """
        Edit the timezone for a quiz channel

        Parameters
        ----------
        channel: The channel to edit
        time_zone: New timezone (e.g., 'US/Eastern', 'Europe/London')
        """
        await ctx.defer(ephemeral=True)

        # Validate the time zone
        try:
            tz = pytz.timezone(time_zone)
        except Exception:
            await ctx.followup.send(
                f"Unknown time zone: {time_zone}. Please use a valid time zone name.",
                ephemeral=True
            )
            return

        async with db_session() as session:
            # Get the quiz channel
            stmt = select(QuizChannel).where(QuizChannel.channel_id == str(channel.id))
            result = await session.execute(stmt)
            quiz_channel = result.scalar_one_or_none()

            if not quiz_channel:
                await ctx.followup.send(
                    f"Channel {channel.mention} is not set up for quizzes. Use `/setup_quiz_channel` first.",
                    ephemeral=True
                )
                return

            # Update timezone
            old_timezone = quiz_channel.time_zone
            quiz_channel.time_zone = time_zone
            session.add(quiz_channel)
            await session.commit()

        await ctx.followup.send(
            f"✅ Updated timezone for {channel.mention} from {old_timezone} to {time_zone}",
            ephemeral=True
        )

    @discord.slash_command(
        name="enable_quiz_posting",
        description="Enable automatic quiz posting for a channel"
    )
    @commands.has_permissions(manage_roles=True)
    async def enable_quiz_posting(
        self,
        ctx,
        channel: Optional[discord.TextChannel] = None
    ):
        """
        Enable automatic quiz posting for a channel

        Parameters
        ----------
        channel: Channel to enable (defaults to current channel)
        """
        await ctx.defer(ephemeral=True)

        # Use current channel if not specified
        if channel is None:
            channel = ctx.channel

        async with db_session() as session:
            # Check if channel is set up
            stmt = select(QuizChannel).where(QuizChannel.channel_id == str(channel.id))
            result = await session.execute(stmt)
            quiz_channel = result.scalar_one_or_none()

            if not quiz_channel:
                await ctx.followup.send(
                    f"❌ {channel.mention} is not set up for quizzes. Use `/setup_quiz_channel` first.",
                    ephemeral=True
                )
                return

            # Enable the channel
            quiz_channel.enabled = True
            session.add(quiz_channel)
            await session.commit()

        await ctx.followup.send(
            f"✅ Quiz posting enabled for {channel.mention}",
            ephemeral=True
        )

    @discord.slash_command(
        name="disable_quiz_posting",
        description="Disable automatic quiz posting for a channel"
    )
    @commands.has_permissions(manage_roles=True)
    async def disable_quiz_posting(
        self,
        ctx,
        channel: Optional[discord.TextChannel] = None
    ):
        """
        Disable automatic quiz posting for a channel

        Parameters
        ----------
        channel: Channel to disable (defaults to current channel)
        """
        await ctx.defer(ephemeral=True)

        # Use current channel if not specified
        if channel is None:
            channel = ctx.channel

        async with db_session() as session:
            # Check if channel is set up
            stmt = select(QuizChannel).where(QuizChannel.channel_id == str(channel.id))
            result = await session.execute(stmt)
            quiz_channel = result.scalar_one_or_none()

            if not quiz_channel:
                await ctx.followup.send(
                    f"❌ {channel.mention} is not set up for quizzes.",
                    ephemeral=True
                )
                return

            # Disable the channel
            quiz_channel.enabled = False
            session.add(quiz_channel)
            await session.commit()

        await ctx.followup.send(
            f"🔴 Quiz posting disabled for {channel.mention}. "
            f"Schedules are preserved and can be re-enabled with `/enable_quiz_posting`.",
            ephemeral=True
        )

    async def get_channel_schedules(self, channel_id) -> List[tuple]:
        """Get all schedules for a channel as (id, post_time) tuples"""
        async with db_session() as session:
            stmt = select(QuizSchedule).where(QuizSchedule.channel_id == str(channel_id))
            result = await session.execute(stmt)
            schedules = result.scalars().all()

            return [(schedule.id, schedule.post_time) for schedule in schedules]

def setup(bot):
    bot.add_cog(QuizSchedulingCog(bot))
