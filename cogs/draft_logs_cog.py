import discord
from discord.ext import commands, tasks
import asyncio
from datetime import datetime
from loguru import logger
from sqlalchemy import select, and_, or_, desc, func
import pytz
from typing import Optional, List
from database.db_session import db_session
from models.draft_logs import LogChannel, BackupLog, UserSubmission, PostSchedule

class DraftLogsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        logger.info("Draft Logs cog initialized")
        self.check_and_post.start()
    
    def cog_unload(self):
        self.check_and_post.cancel()

    @discord.slash_command(
        name="setup_draft_logs",
        description="Set up a channel for posting draft logs"
    )
    @commands.has_permissions(administrator=True)
    async def setup_draft_logs(
        self, 
        ctx,
        channel: discord.TextChannel,
        time_zone: Optional[str] = "UTC"
    ):
        """
        Set up a channel for posting draft logs
        
        Parameters
        ----------
        channel: The channel to post logs in
        time_zone: Time zone (default: UTC)
        """
        await ctx.defer(ephemeral=True)
        
        # Validate the time zone
        try:
            tz = pytz.timezone(time_zone)
        except Exception:
            await ctx.followup.send(f"Unknown time zone: {time_zone}. Please use a valid time zone name (e.g., 'US/Eastern', 'Europe/London').", ephemeral=True)
            return
        
        async with db_session() as session:
            # Check if channel is already set up
            stmt = select(LogChannel).where(LogChannel.channel_id == str(channel.id))
            result = await session.execute(stmt)
            existing_channel = result.scalar_one_or_none()
            
            if existing_channel:
                await ctx.followup.send(f"Channel {channel.mention} is already set up for draft logs. Use `/edit_log_timezone` to modify settings.", ephemeral=True)
                return
            
            # Create new log channel entry
            new_channel = LogChannel(
                channel_id=str(channel.id),
                guild_id=str(ctx.guild.id),
                time_zone=time_zone
            )
            
            session.add(new_channel)
            await session.commit()
        
        await ctx.followup.send(
            f"✅ Successfully set up {channel.mention} for draft logs with time zone {time_zone}. "
            f"Now add posting schedules with `/add_log_schedule` and backup logs with `/add_backup_log`.",
            ephemeral=True
        )

    @discord.slash_command(
        name="add_log_schedule",
        description="Add a posting schedule for draft logs"
    )
    @commands.has_permissions(administrator=True)
    async def add_log_schedule(
        self,
        ctx,
        channel: discord.TextChannel,
        hour: int,
        minute: int
    ):
        """
        Add a posting schedule for draft logs
        
        Parameters
        ----------
        channel: The channel to add a schedule for
        hour: Hour to post (0-23)
        minute: Minute to post (0-59)
        """
        await ctx.defer(ephemeral=True)
        
        # Validate hour and minute
        if hour < 0 or hour > 23:
            await ctx.followup.send("Hour must be between 0 and 23.", ephemeral=True)
            return
            
        if minute < 0 or minute > 59:
            await ctx.followup.send("Minute must be between 0 and 59.", ephemeral=True)
            return
        
        async with db_session() as session:
            # Check if channel is set up
            stmt = select(LogChannel).where(LogChannel.channel_id == str(channel.id))
            result = await session.execute(stmt)
            log_channel = result.scalar_one_or_none()
            
            if not log_channel:
                await ctx.followup.send(f"Channel {channel.mention} is not set up for draft logs. Use `/setup_draft_logs` first.", ephemeral=True)
                return
            
            # Format post time
            post_time = f"{hour:02d}:{minute:02d}"
            
            # Check if schedule already exists
            stmt = select(PostSchedule).where(
                and_(
                    PostSchedule.channel_id == str(channel.id),
                    PostSchedule.post_time == post_time
                )
            )
            result = await session.execute(stmt)
            existing_schedule = result.scalar_one_or_none()
            
            if existing_schedule:
                await ctx.followup.send(f"A schedule for {post_time} already exists for this channel.", ephemeral=True)
                return
            
            # Add new schedule
            new_schedule = PostSchedule(
                channel_id=str(channel.id),
                post_time=post_time
            )
            
            session.add(new_schedule)
            await session.commit()
        
        # Get current schedules to show in response
        schedules = await self.get_channel_schedules(channel.id)
        schedule_list = "\n".join([f"• {schedule}" for schedule in schedules])
        
        await ctx.followup.send(
            f"✅ Added posting schedule for {channel.mention} at {post_time}.\n\n"
            f"Current schedules:\n{schedule_list}",
            ephemeral=True
        )

    @discord.slash_command(
        name="list_log_schedules",
        description="List all posting schedules for a channel"
    )
    @commands.has_permissions(administrator=True)
    async def list_log_schedules(
        self,
        ctx,
        channel: discord.TextChannel
    ):
        """
        List all posting schedules for a channel
        
        Parameters
        ----------
        channel: The channel to list schedules for
        """
        await ctx.defer(ephemeral=True)
        
        schedules = await self.get_channel_schedules(channel.id)
        
        if not schedules:
            await ctx.followup.send(f"No schedules found for {channel.mention}. Add one with `/add_log_schedule`.", ephemeral=True)
            return
        
        schedule_list = "\n".join([f"• ID: {schedule[0]} - Time: {schedule[1]}" for schedule in schedules])
        
        await ctx.followup.send(
            f"Posting schedules for {channel.mention}:\n{schedule_list}",
            ephemeral=True
        )

    @discord.slash_command(
        name="remove_log_schedule",
        description="Remove a posting schedule"
    )
    @commands.has_permissions(administrator=True)
    async def remove_log_schedule(
        self,
        ctx,
        schedule_id: int
    ):
        """
        Remove a posting schedule
        
        Parameters
        ----------
        schedule_id: ID of the schedule to remove
        """
        await ctx.defer(ephemeral=True)
        
        async with db_session() as session:
            # Check if schedule exists
            stmt = select(PostSchedule).where(PostSchedule.id == schedule_id)
            result = await session.execute(stmt)
            schedule = result.scalar_one_or_none()
            
            if not schedule:
                await ctx.followup.send(f"No schedule found with ID {schedule_id}.", ephemeral=True)
                return
            
            # Get channel for confirmation message
            channel_id = schedule.channel_id
            post_time = schedule.post_time
            
            # Delete schedule
            await session.delete(schedule)
            await session.commit()
        
        # Get channel mention for response
        channel = self.bot.get_channel(int(channel_id))
        if channel:
            channel_mention = channel.mention
        else:
            channel_mention = f"channel {channel_id}"
        
        await ctx.followup.send(
            f"✅ Removed posting schedule at {post_time} for {channel_mention}.",
            ephemeral=True
        )

    async def get_channel_schedules(self, channel_id) -> List[tuple]:
        """Get all schedules for a channel as (id, post_time) tuples"""
        async with db_session() as session:
            stmt = select(PostSchedule).where(PostSchedule.channel_id == str(channel_id))
            result = await session.execute(stmt)
            schedules = result.scalars().all()
            
            return [(schedule.id, schedule.post_time) for schedule in schedules]

    @discord.slash_command(
        name="add_backup_log",
        description="Add a backup draft log (Admin only)"
    )
    @commands.has_permissions(administrator=True)
    async def add_backup_log(
        self,
        ctx,
        url: str
    ):
        """
        Add a backup draft log
        
        Parameters
        ----------
        url: URL to the draft log
        """
        await ctx.defer(ephemeral=True)
        
        async with db_session() as session:
            # Find the configured channel for this guild
            stmt = select(LogChannel).where(LogChannel.guild_id == str(ctx.guild.id))
            result = await session.execute(stmt)
            log_channels = result.scalars().all()
            
            if not log_channels:
                await ctx.followup.send("No channels are set up for draft logs in this server. Use `/setup_draft_logs` first.", ephemeral=True)
                return
            
            log_channel = log_channels[0]
            channel = ctx.guild.get_channel(int(log_channel.channel_id))
            
            if not channel:
                await ctx.followup.send("The configured log channel no longer exists. Please set up a new one.", ephemeral=True)
                return
            
            # Add backup log
            backup_log = BackupLog(
                url=url,
                added_by=str(ctx.author.id),
                channel_id=str(log_channel.channel_id)
            )
            
            session.add(backup_log)
            await session.commit()
        
        await ctx.followup.send(f"✅ Added backup draft log to {channel.mention}", ephemeral=True)

    @discord.slash_command(
        name="submit_draft_log",
        description="Submit your MTG draft log"
    )
    async def submit_log(
        self,
        ctx,
        url: str
    ):
        """
        Submit your MTG draft log
        
        Parameters
        ----------
        url: URL to your draft log
        """
        await ctx.defer(ephemeral=True)
        
        async with db_session() as session:
            # Find the configured channel for this guild
            stmt = select(LogChannel).where(LogChannel.guild_id == str(ctx.guild.id))
            result = await session.execute(stmt)
            log_channels = result.scalars().all()
            
            if not log_channels:
                await ctx.followup.send("No channels are set up for draft logs in this server. Please ask an admin to set one up with `/setup_draft_logs`.", ephemeral=True)
                return
            
            # If there are multiple channels, use the first one
            # Alternatively, you could show a selection menu here
            log_channel = log_channels[0]
            channel = ctx.guild.get_channel(int(log_channel.channel_id))
            
            if not channel:
                await ctx.followup.send("The configured log channel no longer exists. Please ask an admin to set up a new one.", ephemeral=True)
                return
            
            # Add user submission
            submission = UserSubmission(
                url=url,
                submitted_by=str(ctx.author.id),
                channel_id=str(log_channel.channel_id)
            )
            
            session.add(submission)
            await session.commit()
        
        await ctx.followup.send(f"✅ Your draft log has been submitted and will be posted anonymously in {channel.mention}. Thank you!", ephemeral=True)

    @discord.slash_command(
        name="post_draft_log_now",
        description="Immediately post a draft log (Admin only)"
    )
    @commands.has_permissions(administrator=True)
    async def post_now(
        self,
        ctx,
        channel: discord.TextChannel
    ):
        """
        Immediately post a draft log
        
        Parameters
        ----------
        channel: Channel to post in
        """
        await ctx.defer(ephemeral=True)
        
        success = await self.post_draft_log(channel.id)
        if success:
            await ctx.followup.send(f"✅ Posted a draft log in {channel.mention}", ephemeral=True)
        else:
            await ctx.followup.send(f"❌ No logs available to post in {channel.mention}", ephemeral=True)

    @discord.slash_command(
        name="list_draft_logs",
        description="List available logs (Admin only)"
    )
    @commands.has_permissions(administrator=True)
    async def list_logs(
        self, 
        ctx, 
        channel: discord.TextChannel,
        log_type: str
    ):
        """
        List available logs
        
        Parameters
        ----------
        channel: Channel to check logs for
        log_type: Type of logs to list (backup or submissions)
        """
        await ctx.defer(ephemeral=True)
        
        async with db_session() as session:
            if log_type == "backup":
                stmt = select(BackupLog).where(BackupLog.channel_id == str(channel.id)).order_by(BackupLog.id)
                result = await session.execute(stmt)
                logs = result.scalars().all()
                title = "Backup Logs"
            else:
                stmt = select(UserSubmission).where(UserSubmission.channel_id == str(channel.id)).order_by(UserSubmission.id)
                result = await session.execute(stmt)
                logs = result.scalars().all()
                title = "User Submissions"
            
            if not logs:
                await ctx.followup.send(f"No {log_type} found for {channel.mention}", ephemeral=True)
                return
            
            # Create an embed to display logs
            embed = discord.Embed(title=f"{title} for {channel.name}", color=discord.Color.blue())
            
            for log in logs:
                status = "✅ Used" if log.used else "⏳ Unused"
                embed.add_field(name=f"ID: {log.id} - {status}", value=log.url[:100], inline=False)
            
            await ctx.followup.send(embed=embed, ephemeral=True)

    @discord.slash_command(
        name="delete_draft_log",
        description="Delete a specific log (Admin only)"
    )
    @commands.has_permissions(administrator=True)
    async def delete_log(
        self,
        ctx,
        log_id: int,
        log_type: str
    ):
        """
        Delete a specific log
        
        Parameters
        ----------
        log_id: ID of the log to delete
        log_type: Type of log to delete (backup or submission)
        """
        await ctx.defer(ephemeral=True)
        
        async with db_session() as session:
            # Get the appropriate model class
            model_class = BackupLog if log_type == "backup" else UserSubmission
            
            # Find the log
            stmt = select(model_class).where(model_class.id == log_id)
            result = await session.execute(stmt)
            log = result.scalar_one_or_none()
            
            if not log:
                await ctx.followup.send(f"No {log_type} found with ID {log_id}", ephemeral=True)
                return
            
            # Delete the log
            await session.delete(log)
            await session.commit()
        
        await ctx.followup.send(f"✅ Deleted {log_type} with ID {log_id}", ephemeral=True)
    
    @discord.slash_command(
        name="reset_draft_logs",
        description="Reset all used flags for logs (Admin only)"
    )
    @commands.has_permissions(administrator=True)
    async def reset_logs(
        self,
        ctx,
        channel: discord.TextChannel,
        log_type: str
    ):
        """
        Reset all used flags for logs
        
        Parameters
        ----------
        channel: Channel to reset logs for
        log_type: Type of logs to reset (backup, submissions, or all)
        """
        await ctx.defer(ephemeral=True)
        
        async with db_session() as session:
            if log_type == "backup" or log_type == "all":
                # Get all backup logs for the channel
                stmt = select(BackupLog).where(BackupLog.channel_id == str(channel.id))
                result = await session.execute(stmt)
                backup_logs = result.scalars().all()
                
                # Reset used flag
                for log in backup_logs:
                    log.used = False
                    session.add(log)
            
            if log_type == "submissions" or log_type == "all":
                # Get all user submissions for the channel
                stmt = select(UserSubmission).where(UserSubmission.channel_id == str(channel.id))
                result = await session.execute(stmt)
                submissions = result.scalars().all()
                
                # Reset used flag
                for submission in submissions:
                    submission.used = False
                    session.add(submission)
            
            await session.commit()
        
        await ctx.followup.send(f"✅ Reset {log_type} logs for {channel.mention}", ephemeral=True)

    async def post_draft_log(self, channel_id):
        """Post a draft log to the specified channel"""
        async with db_session() as session:
            # Try to get an unused user submission first
            stmt = select(UserSubmission).where(
                and_(
                    UserSubmission.channel_id == str(channel_id),
                    UserSubmission.used == False
                )
            ).order_by(func.random()).limit(1)
            
            result = await session.execute(stmt)
            submission = result.scalar_one_or_none()
            
            # If no user submissions, try to get a backup log
            if not submission:
                stmt = select(BackupLog).where(
                    and_(
                        BackupLog.channel_id == str(channel_id),
                        BackupLog.used == False
                    )
                ).order_by(BackupLog.id).limit(1)
                
                result = await session.execute(stmt)
                backup_log = result.scalar_one_or_none()
                
                if not backup_log:
                    # If all logs are used, reset user submissions and try again
                    user_submissions_stmt = select(UserSubmission).where(
                        UserSubmission.channel_id == str(channel_id)
                    )
                    result = await session.execute(user_submissions_stmt)
                    all_submissions = result.scalars().all()
                    
                    if all_submissions:
                        for sub in all_submissions:
                            sub.used = False
                            session.add(sub)
                        await session.commit()
                        
                        # Try again to get a user submission
                        stmt = select(UserSubmission).where(
                            UserSubmission.channel_id == str(channel_id)
                        ).order_by(desc(UserSubmission.submitted_on)).limit(1)
                        
                        result = await session.execute(stmt)
                        submission = result.scalar_one_or_none()
                    
                    # If still no submissions, try resetting backup logs
                    if not submission:
                        backup_logs_stmt = select(BackupLog).where(
                            BackupLog.channel_id == str(channel_id)
                        )
                        result = await session.execute(backup_logs_stmt)
                        all_backup_logs = result.scalars().all()
                        
                        if all_backup_logs:
                            for log in all_backup_logs:
                                log.used = False
                                session.add(log)
                            await session.commit()
                            
                            # Try again to get a backup log
                            stmt = select(BackupLog).where(
                                BackupLog.channel_id == str(channel_id)
                            ).order_by(BackupLog.id).limit(1)
                            
                            result = await session.execute(stmt)
                            backup_log = result.scalar_one_or_none()
                
                # Use the backup log if available
                log_to_use = backup_log
            else:
                # Use the user submission
                log_to_use = submission
            
            # If no logs at all, return False
            if not log_to_use:
                return False
            
            # Mark the log as used
            log_to_use.used = True
            session.add(log_to_use)
            
            # Update the last post time for the channel
            channel_stmt = select(LogChannel).where(LogChannel.channel_id == str(channel_id))
            result = await session.execute(channel_stmt)
            log_channel = result.scalar_one_or_none()
            
            if log_channel:
                log_channel.last_post = datetime.now()
                session.add(log_channel)
            
            await session.commit()
            
            # Post the log to the channel
            discord_channel = self.bot.get_channel(int(channel_id))
            if discord_channel:
                embed = discord.Embed(
                    title="📊 Anonymous Draft Log Review",
                    description="Draft Logs posted twice a day (10am ET / 1pm ET)!",
                    color=discord.Color.purple()
                )
                embed.add_field(name="Draft Log URL", value=log_to_use.url, inline=False)
                embed.set_footer(text="Logs are posted anonymously. Submit your own with /submit_draft_log")
                
                await discord_channel.send(embed=embed)
                return True
            
            return False

    @tasks.loop(minutes=1)
    async def check_and_post(self):
        """Check if it's time to post a log in any channels"""
        try:
            await self.bot.wait_until_ready()
            
            async with db_session() as session:
                # Get all log channels and their schedules
                stmt = select(LogChannel, PostSchedule).join(
                    PostSchedule, LogChannel.channel_id == PostSchedule.channel_id
                )
                result = await session.execute(stmt)
                channel_schedules = result.all()
                
                for log_channel, schedule in channel_schedules:
                    try:
                        # Get current time in the channel's time zone
                        tz = pytz.timezone(log_channel.time_zone)
                        current_time = datetime.now(tz).strftime("%H:%M")
                        
                        # Check if it's time to post
                        if current_time == schedule.post_time:
                            logger.info(f"Posting draft log in channel {log_channel.channel_id} at {current_time} {log_channel.time_zone}")
                            await self.post_draft_log(log_channel.channel_id)
                    except Exception as e:
                        logger.error(f"Error posting scheduled log to channel {log_channel.channel_id}: {e}")
                        
        except Exception as e:
            logger.error(f"Error in check_and_post task: {e}")

def setup(bot):
    bot.add_cog(DraftLogsCog(bot))