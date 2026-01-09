from discord.ext import commands
import os
import discord
from loguru import logger
from config import get_config, save_config
from leaderboard_config import CROWN_ICONS, DEFAULT_CROWN_ROLE_NAMES

from helpers.permissions import has_bot_manager_role, ADMIN_ROLE_NAME


class AdminCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @discord.slash_command(
        name='reload', 
        description='Reload a specific extension'
    )
    @has_bot_manager_role()
    async def reload_extension(self, ctx, extension: str):
        await ctx.defer(ephemeral=True)
        try:
            self.bot.reload_extension(f"cogs.{extension}")
            await ctx.followup.send(f"🔄 Reloaded extension: {extension}", ephemeral=True)
        except Exception as e:
            await ctx.followup.send(f"❌ Error reloading {extension}: {str(e)}", ephemeral=True)

    @discord.slash_command(
        name='load', 
        description='Load a new extension'
    )
    @has_bot_manager_role()
    async def load_extension(self, ctx, extension: str):
        await ctx.defer(ephemeral=True)
        try:
            self.bot.load_extension(f"cogs.{extension}")
            await ctx.followup.send(f"✅ Loaded extension: {extension}", ephemeral=True)
        except Exception as e:
            await ctx.followup.send(f"❌ Error loading {extension}: {str(e)}", ephemeral=True)

    @discord.slash_command(
        name='unload', 
        description='Unload an extension'
    )
    @has_bot_manager_role()
    async def unload_extension(self, ctx, extension: str):
        await ctx.defer(ephemeral=True)
        try:
            self.bot.unload_extension(f"cogs.{extension}")
            await ctx.followup.send(f"❎ Unloaded extension: {extension}", ephemeral=True)
        except Exception as e:
            await ctx.followup.send(f"❌ Error unloading {extension}: {str(e)}", ephemeral=True)

    @discord.slash_command(
        name='reloadall', 
        description='Reload all extensions'
    )
    @has_bot_manager_role()
    async def reload_all_extensions(self, ctx):
        await ctx.defer(ephemeral=True)
        response_messages = []
        try:
            for filename in os.listdir("./cogs"):
                if filename.endswith(".py") and not filename.startswith("_"):
                    extension = filename[:-3]  # Remove .py
                    try:
                        self.bot.reload_extension(f"cogs.{extension}")
                        response_messages.append(f"🔄 Reloaded extension: {extension}")
                    except Exception as e:
                        response_messages.append(f"❌ Error reloading {extension}: {str(e)}")
            
            await ctx.followup.send("\n".join(response_messages), ephemeral=True)
        except Exception as e:
            await ctx.followup.send(f"❌ Error reloading extensions: {str(e)}", ephemeral=True)

    @discord.slash_command(
        name='setup_bot_manager',
        description='Create the Bot Manager role'
    )
    @commands.is_owner()  # Only the owner can set up the role
    async def setup_bot_manager(self, ctx):
        await ctx.defer(ephemeral=True)
        try:
            # Check if role already exists
            role = discord.utils.get(ctx.guild.roles, name=ADMIN_ROLE_NAME)
            if role:
                await ctx.followup.send(f"The {ADMIN_ROLE_NAME} role already exists!", ephemeral=True)
                return

            # Create the role with a distinctive color (purple in this case)
            role = await ctx.guild.create_role(
                name=ADMIN_ROLE_NAME,
                color=discord.Color.purple(),
                reason="Bot Manager role creation"
            )
            logger.info(f"Created {ADMIN_ROLE_NAME} role in {ctx.guild.name}")
            await ctx.followup.send(
                f"✅ Created the {ADMIN_ROLE_NAME} role! You can now assign it to trusted users.", 
                ephemeral=True
            )
        except Exception as e:
            await ctx.followup.send(f"❌ Error creating role: {str(e)}", ephemeral=True)

    @discord.slash_command(
        name='add_bot_manager',
        description='Add a user as a bot manager'
    )
    @commands.is_owner()  # Only the owner can add managers
    async def add_bot_manager(self, ctx, user: discord.Member):
        await ctx.defer(ephemeral=True)
        try:
            role = discord.utils.get(ctx.guild.roles, name=ADMIN_ROLE_NAME)
            if not role:
                await ctx.followup.send(
                    "Bot Manager role doesn't exist! Use `/setup_bot_manager` first.", 
                    ephemeral=True
                )
                return

            if role in user.roles:
                await ctx.followup.send(f"{user.mention} already has the Bot Manager role!", ephemeral=True)
                return

            await user.add_roles(role)
            logger.info(f"Added {user.name} as Bot Manager in {ctx.guild.name}")
            await ctx.followup.send(f"✅ Added {user.mention} as a Bot Manager", ephemeral=True)
        except Exception as e:
            await ctx.followup.send(f"❌ Error adding role: {str(e)}", ephemeral=True)

    @discord.slash_command(
        name='remove_bot_manager',
        description='Remove a user from bot managers'
    )
    @commands.is_owner()  # Only the owner can remove managers
    async def remove_bot_manager(self, ctx, user: discord.Member):
        await ctx.defer(ephemeral=True)
        try:
            role = discord.utils.get(ctx.guild.roles, name=ADMIN_ROLE_NAME)
            if not role:
                await ctx.followup.send("Bot Manager role doesn't exist!", ephemeral=True)
                return

            if role not in user.roles:
                await ctx.followup.send(f"{user.mention} doesn't have the Bot Manager role!", ephemeral=True)
                return

            await user.remove_roles(role)
            logger.info(f"Removed {user.name} from Bot Managers in {ctx.guild.name}")
            await ctx.followup.send(f"✅ Removed {user.mention} from Bot Managers", ephemeral=True)
        except Exception as e:
            await ctx.followup.send(f"❌ Error removing role: {str(e)}", ephemeral=True)

    @discord.slash_command(
        name='post_message',
        description='Post a message to any channel as the bot'
    )
    @has_bot_manager_role()
    async def post_message(self, ctx, channel: discord.TextChannel, message: str):
        """Allow Bot Managers to post messages to any channel via the bot"""
        await ctx.defer(ephemeral=True)
        try:
            # Check if bot has permission to send messages in the target channel
            bot_member = ctx.guild.get_member(self.bot.user.id)
            permissions = channel.permissions_for(bot_member)

            if not permissions.send_messages:
                await ctx.followup.send(
                    f"Bot doesn't have permission to send messages in {channel.mention}",
                    ephemeral=True
                )
                return

            # Send the message to the target channel
            await channel.send(message)

            logger.info(f"Bot Manager {ctx.author.name} ({ctx.author.id}) posted message to {channel.name} in {ctx.guild.name}")
            await ctx.followup.send(
                f"✅ Message posted to {channel.mention}",
                ephemeral=True
            )
        except discord.Forbidden:
            await ctx.followup.send(
                f"Bot doesn't have permission to send messages in {channel.mention}",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error posting message to channel: {e}")
            await ctx.followup.send(f"❌ Error posting message: {str(e)}", ephemeral=True)

    @discord.slash_command(
        name='post_announcement',
        description='Post the announcement from announcement.md file'
    )
    @has_bot_manager_role()
    async def post_announcement(self, ctx, channel: discord.TextChannel):
        """Post an announcement from the announcement.md file"""
        await ctx.defer(ephemeral=True)
        try:
            # Read the announcement file
            announcement_path = "announcement.md"
            with open(announcement_path, 'r', encoding='utf-8') as f:
                announcement = f.read()

            # Check if bot has permission to send messages in the target channel
            bot_member = ctx.guild.get_member(self.bot.user.id)
            permissions = channel.permissions_for(bot_member)

            if not permissions.send_messages:
                await ctx.followup.send(
                    f"Bot doesn't have permission to send messages in {channel.mention}",
                    ephemeral=True
                )
                return

            # Send the announcement to the target channel
            await channel.send(announcement)

            logger.info(f"Bot Manager {ctx.author.name} ({ctx.author.id}) posted announcement to {channel.name} in {ctx.guild.name}")
            await ctx.followup.send(
                f"✅ Announcement posted to {channel.mention}",
                ephemeral=True
            )
        except FileNotFoundError:
            await ctx.followup.send(
                f"❌ Announcement file not found: {announcement_path}\nMake sure announcement.md exists in the bot directory.",
                ephemeral=True
            )
        except discord.Forbidden:
            await ctx.followup.send(
                f"Bot doesn't have permission to send messages in {channel.mention}",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error posting announcement: {e}")
            await ctx.followup.send(f"❌ Error posting announcement: {str(e)}", ephemeral=True)

    @discord.slash_command(
        name='setup_crown_roles',
        description='Create the crown roles for the leaderboard crown system'
    )
    @has_bot_manager_role()
    async def setup_crown_roles(self, ctx):
        """Create the Discord roles needed for the crown system"""
        await ctx.defer(ephemeral=True)

        config = get_config(ctx.guild.id)
        crown_config = config.get("crown_roles", {})

        # Get role names from config or use defaults from leaderboard_config
        role_names = crown_config.get("role_names", DEFAULT_CROWN_ROLE_NAMES)

        # Convert CROWN_ICONS to string keys for role creation (Discord API uses strings)
        role_icons = {str(k): v for k, v in CROWN_ICONS.items()}

        # Check if server supports role icons (boost level 2+)
        supports_icons = ctx.guild.premium_tier >= 2

        created_roles = []
        existing_roles = []
        failed_roles = []

        # Create roles in reverse order so higher crowns appear higher in role list
        for count in sorted(role_names.keys(), key=lambda x: int(x), reverse=True):
            role_name = role_names[count]

            # Check if role already exists
            existing_role = discord.utils.get(ctx.guild.roles, name=role_name)
            if existing_role:
                existing_roles.append(role_name)
                continue

            try:
                # Build role creation kwargs
                role_kwargs = {
                    "name": role_name,
                    "color": discord.Color.gold(),
                    "hoist": True,  # Show separately in member list
                    "mentionable": False,
                    "reason": f"Crown role created by {ctx.author.name}"
                }

                # Add icon if server supports it
                if supports_icons and count in role_icons:
                    role_kwargs["display_icon"] = role_icons[count]

                await ctx.guild.create_role(**role_kwargs)
                created_roles.append(role_name)
                logger.info(f"Created crown role '{role_name}' in {ctx.guild.name}")
            except discord.Forbidden:
                failed_roles.append(role_name)
                logger.warning(f"Failed to create role '{role_name}' - missing permissions")
            except Exception as e:
                failed_roles.append(role_name)
                logger.error(f"Error creating role '{role_name}': {e}")

        # Build response message
        response_parts = []
        if created_roles:
            icon_note = " (with icons)" if supports_icons else ""
            response_parts.append(f"✅ Created roles{icon_note}: {', '.join(created_roles)}")
        if existing_roles:
            response_parts.append(f"ℹ️ Already exist: {', '.join(existing_roles)}")
        if failed_roles:
            response_parts.append(f"❌ Failed to create: {', '.join(failed_roles)}")

        if not supports_icons and created_roles:
            response_parts.append("\n💡 Tip: Boost to level 2 to get role icons!")

        # Check if crown roles are enabled
        if not crown_config.get("enabled", False):
            response_parts.append("\n⚠️ Crown roles are not yet enabled. Use `/enable_crown_roles` to enable them.")

        await ctx.followup.send("\n".join(response_parts), ephemeral=True)

    @discord.slash_command(
        name='enable_crown_roles',
        description='Enable or disable the crown roles system for this server'
    )
    @has_bot_manager_role()
    async def enable_crown_roles(self, ctx, enabled: bool):
        """Enable or disable crown roles for this guild"""
        from config import get_config, save_config

        await ctx.defer(ephemeral=True)

        config = get_config(ctx.guild.id)

        # Ensure crown_roles section exists
        if "crown_roles" not in config:
            config["crown_roles"] = {
                "enabled": False,
                "eligible_categories": [
                    "draft_record",
                    "match_win",
                    "drafts_played",
                    "time_vault_and_key",
                    "quiz_points"
                ],
                "timeframe": "lifetime",
                "role_names": {
                    "1": "Crown",
                    "2": "Double Crown",
                    "3": "Triple Crown",
                    "4": "Grand Champion",
                    "5": "Ultimate Champion"
                }
            }

        config["crown_roles"]["enabled"] = enabled
        save_config(ctx.guild.id, config)

        if enabled:
            await ctx.followup.send(
                "✅ Crown roles are now **enabled**.\n"
                "Crown roles will be updated after each draft completes.\n"
                "Use `/refresh_crown_roles` to update them now.",
                ephemeral=True
            )
        else:
            await ctx.followup.send("❌ Crown roles are now **disabled**.", ephemeral=True)

        logger.info(f"Crown roles {'enabled' if enabled else 'disabled'} for guild {ctx.guild.name} by {ctx.author.name}")

    @discord.slash_command(
        name='refresh_crown_roles',
        description='Manually refresh crown roles based on current leaderboard standings'
    )
    @has_bot_manager_role()
    async def refresh_crown_roles(self, ctx):
        """Manually trigger a crown role refresh"""
        from config import get_config
        from services.crown_roles import update_crown_roles_for_guild

        await ctx.defer(ephemeral=True)

        config = get_config(ctx.guild.id)
        crown_config = config.get("crown_roles", {})

        if not crown_config.get("enabled", False):
            await ctx.followup.send(
                "❌ Crown roles are not enabled for this server.\n"
                "Use `/enable_crown_roles True` to enable them first.",
                ephemeral=True
            )
            return

        try:
            await update_crown_roles_for_guild(self.bot, str(ctx.guild.id))
            await ctx.followup.send("✅ Crown roles have been refreshed!", ephemeral=True)
            logger.info(f"Crown roles manually refreshed for guild {ctx.guild.name} by {ctx.author.name}")
        except Exception as e:
            logger.error(f"Error refreshing crown roles: {e}")
            await ctx.followup.send(f"❌ Error refreshing crown roles: {str(e)}", ephemeral=True)

    @discord.slash_command(
        name='set_crown_timeframe',
        description='Set the timeframe for crown role calculations'
    )
    @has_bot_manager_role()
    async def set_crown_timeframe(
        self,
        ctx,
        timeframe: discord.Option(
            str,
            description="The timeframe for crown calculations",
            choices=["14d", "30d", "90d", "lifetime"]
        )
    ):
        """Set the timeframe used for crown role leaderboard calculations"""
        from config import get_config, save_config
        from services.crown_roles import update_crown_roles_for_guild

        await ctx.defer(ephemeral=True)

        config = get_config(ctx.guild.id)

        # Ensure crown_roles section exists
        if "crown_roles" not in config:
            await ctx.followup.send(
                "❌ Crown roles are not configured. Use `/enable_crown_roles True` first.",
                ephemeral=True
            )
            return

        # Update the timeframe
        old_timeframe = config["crown_roles"].get("timeframe", "30d")
        config["crown_roles"]["timeframe"] = timeframe
        save_config(ctx.guild.id, config)

        # Map timeframe values to readable labels
        timeframe_labels = {
            "14d": "14 Days",
            "30d": "30 Days",
            "90d": "90 Days",
            "lifetime": "Lifetime (all-time)"
        }

        response = f"✅ Crown timeframe changed from **{timeframe_labels.get(old_timeframe, old_timeframe)}** to **{timeframe_labels.get(timeframe, timeframe)}**."

        # If crown roles are enabled, offer to refresh
        if config["crown_roles"].get("enabled", False):
            try:
                await update_crown_roles_for_guild(self.bot, str(ctx.guild.id))
                response += "\n\nCrown roles have been refreshed with the new timeframe."
            except Exception as e:
                logger.error(f"Error refreshing crown roles after timeframe change: {e}")
                response += "\n\n⚠️ Could not refresh crown roles automatically. Use `/refresh_crown_roles` to update."

        await ctx.followup.send(response, ephemeral=True)
        logger.info(f"Crown timeframe changed to {timeframe} for guild {ctx.guild.name} by {ctx.author.name}")

    @discord.slash_command(
        name='set_leaderboard_channel',
        description='Set the channel where leaderboards will be posted'
    )
    @has_bot_manager_role()
    async def set_leaderboard_channel(self, ctx, channel: discord.TextChannel):
        """Set the channel for leaderboard posts"""
        from database.db_session import db_session
        from models import LeaderboardMessage
        from sqlalchemy import select

        await ctx.defer(ephemeral=True)

        guild_id = str(ctx.guild.id)

        try:
            async with db_session() as session:
                # Find existing leaderboard record
                stmt = select(LeaderboardMessage).where(LeaderboardMessage.guild_id == guild_id)
                result = await session.execute(stmt)
                leaderboard_record = result.scalar_one_or_none()

                if leaderboard_record:
                    # Update existing record
                    leaderboard_record.channel_id = str(channel.id)

                    # Reset all message IDs so new messages are created in the new channel
                    leaderboard_record.message_id = "placeholder"
                    leaderboard_record.draft_record_view_message_id = None
                    leaderboard_record.match_win_view_message_id = None
                    leaderboard_record.drafts_played_view_message_id = None
                    leaderboard_record.time_vault_and_key_view_message_id = None
                    leaderboard_record.longest_win_streak_view_message_id = None
                    leaderboard_record.perfect_streak_view_message_id = None
                    leaderboard_record.quiz_points_view_message_id = None
                    leaderboard_record.draft_win_streak_view_message_id = None

                    await session.commit()
                    logger.info(f"Leaderboard channel changed to {channel.name} for guild {ctx.guild.name} by {ctx.author.name}")
                    await ctx.followup.send(
                        f"✅ Leaderboard channel set to {channel.mention}\n"
                        f"Run `/leaderboard` to create new leaderboard messages in this channel.",
                        ephemeral=True
                    )
                else:
                    # No existing record - create one
                    new_record = LeaderboardMessage(
                        guild_id=guild_id,
                        channel_id=str(channel.id),
                        message_id="placeholder"
                    )
                    session.add(new_record)
                    await session.commit()
                    logger.info(f"Leaderboard channel set to {channel.name} for guild {ctx.guild.name} by {ctx.author.name}")
                    await ctx.followup.send(
                        f"✅ Leaderboard channel set to {channel.mention}\n"
                        f"Run `/leaderboard` to create leaderboard messages in this channel.",
                        ephemeral=True
                    )

        except Exception as e:
            logger.error(f"Error setting leaderboard channel: {e}")
            await ctx.followup.send(f"❌ Error setting leaderboard channel: {str(e)}", ephemeral=True)


def setup(bot):
    bot.add_cog(AdminCommands(bot)) 