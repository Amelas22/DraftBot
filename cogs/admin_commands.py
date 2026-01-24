from discord.ext import commands
import asyncio
import os
from pathlib import Path
import discord
from loguru import logger
from config import get_config, save_config
from leaderboard_config import CROWN_ICONS, DEFAULT_CROWN_ROLE_NAMES
from stats_display import get_stats_embed_for_player

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
        updated_roles = []
        errors = []

        # Create/update roles in reverse order so higher crowns appear higher in role list
        for count in sorted(role_names.keys(), key=lambda x: int(x), reverse=True):
            role_name = role_names[count]

            # Prepare role properties
            role_kwargs = {
                "color": discord.Color.gold(),
                "hoist": True,  # Show separately in member list
                "mentionable": False,
            }

            # Try to use custom image for role icon if available
            image_used = False
            if supports_icons:
                image_path = Path("images") / f"{count}crown.png"
                if image_path.exists():
                    try:
                        with open(image_path, 'rb') as image_file:
                            image_data = image_file.read()
                            role_kwargs["icon"] = image_data
                            image_used = True
                            logger.debug(f"Using custom image for role '{role_name}' from {image_path}")
                    except Exception as e:
                        logger.warning(f"Failed to read image {image_path}: {e}")

            # Fallback to unicode emoji if no image was used
            if not image_used and supports_icons and count in role_icons:
                role_kwargs["unicode_emoji"] = role_icons[count]

            # Check if role already exists
            existing_role = discord.utils.get(ctx.guild.roles, name=role_name)

            if existing_role:
                # Role exists - update its properties
                try:
                    edit_kwargs = {
                        "color": role_kwargs["color"],
                        "hoist": role_kwargs["hoist"],
                        "mentionable": role_kwargs["mentionable"],
                        "reason": f"Crown role updated by {ctx.author.name}"
                    }
                    # Add icon or unicode_emoji depending on what was prepared
                    if "icon" in role_kwargs:
                        edit_kwargs["icon"] = role_kwargs["icon"]
                    elif "unicode_emoji" in role_kwargs:
                        edit_kwargs["unicode_emoji"] = role_kwargs["unicode_emoji"]

                    await existing_role.edit(**edit_kwargs)
                    updated_roles.append(role_name)
                    logger.info(f"Updated crown role '{role_name}' in {ctx.guild.name}")
                except discord.HTTPException as e:
                    logger.error(f"Failed to update role '{role_name}': {e}")
                    errors.append(f"{role_name}: {e}")
            else:
                # Role doesn't exist - create it
                try:
                    role_kwargs["name"] = role_name
                    role_kwargs["reason"] = f"Crown role created by {ctx.author.name}"
                    await ctx.guild.create_role(**role_kwargs)
                    created_roles.append(role_name)
                    logger.info(f"Created crown role '{role_name}' in {ctx.guild.name}")
                except discord.HTTPException as e:
                    logger.error(f"Failed to create role '{role_name}': {e}")
                    errors.append(f"{role_name}: {e}")

        # Build response message
        response_parts = []

        if created_roles:
            created_list = ", ".join(created_roles)
            if supports_icons:
                response_parts.append(f"✅ **Created roles** (with icons): {created_list}")
            else:
                response_parts.append(f"✅ **Created roles**: {created_list}")

        if updated_roles:
            updated_list = ", ".join(updated_roles)
            if supports_icons:
                response_parts.append(f"🔄 **Updated roles** (with new icons): {updated_list}")
            else:
                response_parts.append(f"🔄 **Updated roles**: {updated_list}")

        if errors:
            error_summary = f"⚠️ **Errors:** {len(errors)}"
            for error in errors[:3]:  # Show first 3
                error_summary += f"\n• {error}"
            if len(errors) > 3:
                error_summary += f"\n• ...and {len(errors) - 3} more (check logs)"
            response_parts.append(error_summary)

        if not created_roles and not updated_roles and not errors:
            response_parts.append("ℹ️ No changes needed - all roles already configured correctly")

        # Add boost level note
        if not supports_icons:
            response_parts.append("\n💡 **Tip**: Boost to level 2+ to enable role icons!")

        # Check if crown roles are enabled
        if not crown_config.get("enabled", False):
            response_parts.append("\n⚠️ Crown roles are not yet enabled. Use `/enable_crown_roles` to enable them.")

        await ctx.followup.send("\n\n".join(response_parts), ephemeral=True)

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

    @discord.slash_command(
        name='test_disconnect',
        description='[TEST] Simulate a connection failure for an active draft session'
    )
    @has_bot_manager_role()
    async def test_disconnect(self, ctx):
        """Simulate a Draftmancer connection failure to test the notification system."""
        from config import TEST_MODE_ENABLED
        from services.draft_setup_manager import ACTIVE_MANAGERS

        await ctx.defer(ephemeral=True)

        if not TEST_MODE_ENABLED:
            await ctx.followup.send(
                "❌ This command is only available when TEST_MODE_ENABLED is True in config.py",
                ephemeral=True
            )
            return

        # Find active managers for this guild
        guild_managers = []
        for session_id, manager in ACTIVE_MANAGERS.items():
            if manager.guild_id == str(ctx.guild.id):
                guild_managers.append((session_id, manager))

        if not guild_managers:
            await ctx.followup.send(
                "❌ No active Draftmancer connections found for this guild.\n\n"
                "**Note:** The bot only connects to Draftmancer after:\n"
                "1. A draft has full signups\n"
                "2. Ready check passes\n"
                "3. Teams are created and links are distributed\n\n"
                "To test, you need a draft that has reached the 'links distributed' stage.",
                ephemeral=True
            )
            return

        # Show the active sessions and let the user know what will happen
        session_list = "\n".join([f"• `{sid}` (draft_id: {mgr.draft_id})" for sid, mgr in guild_managers])
        await ctx.followup.send(
            f"[TEST] Found {len(guild_managers)} active draft session(s):\n{session_list}\n\n"
            f"Simulating connection failure for the first one...",
            ephemeral=True
        )

        # Get the first manager and test auto-recovery
        session_id, manager = guild_managers[0]

        # Log initial state
        logger.info(f"[TEST] === DISCONNECT TEST START ===")
        logger.info(f"[TEST] Target session: {session_id}")
        logger.info(f"[TEST] Manager instance ID: {id(manager)}")
        logger.info(f"[TEST] Socket instance ID: {id(manager.socket_client)}")
        logger.info(f"[TEST] Initial socket connected: {manager.socket_client.connected}")
        logger.info(f"[TEST] Initial in ACTIVE_MANAGERS: {session_id in ACTIVE_MANAGERS}")

        # Disconnect the socket (simulates connection loss)
        if manager.socket_client.connected:
            await manager.socket_client.disconnect()
            logger.info(f"[TEST] Disconnected socket for session {session_id}")

            await ctx.followup.send(
                f"[TEST] 🔌 Disconnected! Monitoring auto-recovery...\n"
                f"The bot should automatically:\n"
                f"1. Detect disconnect (within 10s loop interval)\n"
                f"2. Reconnect and reclaim ownership\n"
                f"3. Re-import cube and continue managing",
                ephemeral=True
            )

            # Wait and monitor recovery (bot loop has 10s interval, so wait 12s)
            logger.info(f"[TEST] Waiting 12 seconds for recovery...")
            await asyncio.sleep(12)

            # Check state after waiting
            logger.info(f"[TEST] === POST-WAIT STATE CHECK ===")
            logger.info(f"[TEST] Manager instance ID (same?): {id(manager)}")
            logger.info(f"[TEST] Socket connected: {manager.socket_client.connected}")
            logger.info(f"[TEST] Session in ACTIVE_MANAGERS: {session_id in ACTIVE_MANAGERS}")

            if session_id in ACTIVE_MANAGERS:
                current_manager = ACTIVE_MANAGERS[session_id]
                logger.info(f"[TEST] Current manager in ACTIVE_MANAGERS - ID: {id(current_manager)}")
                logger.info(f"[TEST] Is same instance: {current_manager is manager}")
                logger.info(f"[TEST] Current manager socket connected: {current_manager.socket_client.connected}")

            # Check if bot recovered
            if manager.socket_client.connected and session_id in ACTIVE_MANAGERS:
                # Give it 2 more seconds to finish cube import
                await asyncio.sleep(2)

                logger.info(f"[TEST] === RECOVERY SUCCESS REPORTED ===")
                logger.info(f"[TEST] Manager instance ID: {id(manager)}")
                logger.info(f"[TEST] Socket connected: {manager.socket_client.connected}")
                logger.info(f"[TEST] Cube imported: {manager.cube_imported}")
                logger.info(f"[TEST] Settings updated: {manager.settings_updated}")

                await ctx.followup.send(
                    f"[TEST] ✅ **Auto-recovery successful!**\n"
                    f"• Reconnected: {manager.socket_client.connected}\n"
                    f"• Still in active managers: {session_id in ACTIVE_MANAGERS}\n"
                    f"• Cube imported: {manager.cube_imported}\n"
                    f"• Settings updated: {manager.settings_updated}\n\n"
                    f"The bot is continuing to manage the draft normally.",
                    ephemeral=True
                )
            else:
                logger.info(f"[TEST] === RECOVERY FAILURE REPORTED ===")
                logger.info(f"[TEST] Manager socket connected: {manager.socket_client.connected}")
                logger.info(f"[TEST] Session in ACTIVE_MANAGERS: {session_id in ACTIVE_MANAGERS}")

                await ctx.followup.send(
                    f"[TEST] ⚠️ **Auto-recovery failed**\n"
                    f"• Reconnected: {manager.socket_client.connected}\n"
                    f"• Still in active managers: {session_id in ACTIVE_MANAGERS}\n"
                    f"Check logs for details.",
                    ephemeral=True
                )

            logger.info(f"[TEST] === DISCONNECT TEST END ===")
        else:
            await ctx.followup.send(
                "❌ Manager was not connected. Nothing to test.",
                ephemeral=True
            )

    async def _get_expected_match_count(self, draft_session):
        """
        Calculate how many matches should exist for a draft session.
        Returns the match_counter - 1 (since counter tracks next match number).
        """
        if not draft_session.match_counter:
            return 0
        return draft_session.match_counter - 1

    async def _create_missing_matches_as_draws(self, db_session, draft_session):
        """
        Ensure all expected matches exist as draw records.
        For non-Swiss: 3 rounds with team_a_size matches each
        For Swiss: up to 12 matches based on match_counter
        Returns count of matches created.
        """
        from models.match import MatchResult
        from sqlalchemy import select

        # Get existing matches
        stmt = select(MatchResult).where(
            MatchResult.session_id == draft_session.session_id
        )
        result = await db_session.execute(stmt)
        existing_matches = result.scalars().all()
        existing_match_numbers = {m.match_number for m in existing_matches}

        # Calculate expected match count
        expected_count = await self._get_expected_match_count(draft_session)

        # Create missing matches as draws
        created_count = 0
        for match_num in range(1, expected_count + 1):
            if match_num not in existing_match_numbers:
                # Match is missing, create it as a draw
                # Note: We don't have player info, so set both player IDs to None
                match_result = MatchResult(
                    session_id=draft_session.session_id,
                    match_number=match_num,
                    player1_id=None,
                    player2_id=None,
                    player1_wins=0,
                    player2_wins=0,
                    winner_id=None,
                    guild_id=draft_session.guild_id
                )
                db_session.add(match_result)
                created_count += 1
                logger.info(f"Created missing match {match_num} as draw for session {draft_session.session_id}")

        return created_count

    async def _get_unplayed_match_count(self, db_session, session_id: str):
        """
        Count unplayed matches (winner_id=None, both wins=0).
        These will be preserved as draws.
        """
        from sqlalchemy import select, and_, func
        from models.match import MatchResult

        stmt = select(func.count()).where(
            and_(
                MatchResult.session_id == session_id,
                MatchResult.winner_id.is_(None),
                MatchResult.player1_wins == 0,
                MatchResult.player2_wins == 0
            )
        )
        result = await db_session.execute(stmt)
        return result.scalar()

    @discord.slash_command(name='cleanup_stale_drafts', description='Complete old stale drafts as draws and clean up channels')
    @has_bot_manager_role()
    async def cleanup_stale_drafts(
        self,
        ctx,
        hours_old: discord.Option(int, "Complete drafts older than this many hours (minimum 12)", default=24),
        dry_run: discord.Option(bool, "Preview without actually completing/deleting", default=True)
    ):
        """Complete old stale drafts as draws, clean up channels, and remove database records."""
        from config import is_cleanup_exempt
        from datetime import datetime, timedelta
        from session import AsyncSessionLocal
        from models.draft_session import DraftSession

        await ctx.defer(ephemeral=True)

        # Safety: Minimum age requirement
        MIN_HOURS_OLD = 12
        if hours_old < MIN_HOURS_OLD:
            await ctx.followup.send(
                f"❌ Safety check failed: Must clean drafts older than {MIN_HOURS_OLD} hours.\n"
                f"You specified: {hours_old} hours\n\n"
                f"This prevents accidental cleanup of active or recent drafts.",
                ephemeral=True
            )
            return

        # Optional: Check guild cleanup exemption
        if is_cleanup_exempt(ctx.guild.id):
            await ctx.followup.send(
                "❌ This guild is exempt from cleanup operations.\n"
                "Check config.py timeout settings for this guild.",
                ephemeral=True
            )
            return

        # Calculate cutoff time
        cutoff_time = datetime.now() - timedelta(hours=hours_old)

        # Query for old draft sessions
        async with AsyncSessionLocal() as db_session:
            from sqlalchemy import select
            stmt = select(DraftSession).filter(
                DraftSession.guild_id == str(ctx.guild.id),
                DraftSession.draft_start_time < cutoff_time
            )

            result = await db_session.execute(stmt)
            old_sessions = result.scalars().all()

            if not old_sessions:
                await ctx.followup.send(
                    f"✅ No drafts found older than {hours_old} hours in this guild.",
                    ephemeral=True
                )
                return

            # Collect statistics
            from models.match import MatchResult
            from sqlalchemy import func

            total_sessions = len(old_sessions)
            total_channels = 0
            channel_names = []

            # Collect match and completion statistics
            total_matches_to_create = 0
            total_unplayed_matches = 0
            sessions_to_complete = []

            for session in old_sessions:
                if session.channel_ids:
                    total_channels += len(session.channel_ids)
                    # Collect channel names for preview
                    for channel_id in session.channel_ids:
                        channel = ctx.guild.get_channel(int(channel_id))
                        if channel:
                            channel_names.append(channel.name)

                # Calculate how many matches need to be created
                expected = await self._get_expected_match_count(session)
                if expected > 0:
                    # Count existing matches
                    stmt = select(func.count()).where(MatchResult.session_id == session.session_id)
                    result = await db_session.execute(stmt)
                    existing = result.scalar() or 0
                    missing = max(0, expected - existing)
                    total_matches_to_create += missing

                    # Count unplayed matches that will remain as draws
                    unplayed = await self._get_unplayed_match_count(db_session, session.session_id)
                    total_unplayed_matches += unplayed

                    if missing > 0 or unplayed > 0 or session.session_stage != 'completed':
                        sessions_to_complete.append({
                            'session_id': session.session_id,
                            'missing_matches': missing,
                            'unplayed_matches': unplayed,
                            'is_completed': session.session_stage == 'completed'
                        })

            # Show preview
            preview_msg = (
                f"**{'DRY RUN - ' if dry_run else ''}Cleanup Preview**\n\n"
                f"**Target:** Drafts older than {hours_old} hours\n"
                f"**Guild:** {ctx.guild.name}\n"
                f"**Cutoff time:** {cutoff_time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"**Will process:**\n"
                f"• {total_sessions} draft session(s)\n"
                f"• {total_channels} channel(s) will be deleted\n"
            )

            incomplete_count = sum(1 for s in sessions_to_complete if not s['is_completed'])
            if incomplete_count > 0:
                preview_msg += f"• {incomplete_count} draft(s) will be marked as completed\n"

            if total_matches_to_create > 0:
                preview_msg += f"\n**Missing matches (will be created as draws):**\n"
                preview_msg += f"• {total_matches_to_create} match record(s) will be created\n"

            if total_unplayed_matches > 0:
                preview_msg += f"\n**Unplayed matches (already draws):**\n"
                preview_msg += f"• {total_unplayed_matches} existing unplayed match(es)\n"
                preview_msg += f"• These will remain in database as 0-0 draws\n"

            if sessions_to_complete:
                preview_msg += f"\n**Sample sessions to complete:**\n"
                for detail in sessions_to_complete[:5]:
                    status = "completed" if detail['is_completed'] else "incomplete"
                    preview_msg += (
                        f"• Session {detail['session_id'][:8]}... ({status}): "
                        f"{detail['missing_matches']} to create, "
                        f"{detail['unplayed_matches']} unplayed\n"
                    )
                if len(sessions_to_complete) > 5:
                    preview_msg += f"• ...and {len(sessions_to_complete) - 5} more\n"

            preview_msg += "\n"

            if channel_names:
                sample_channels = channel_names[:10]
                preview_msg += f"**Channel examples:**\n"
                for name in sample_channels:
                    preview_msg += f"• {name}\n"
                if len(channel_names) > 10:
                    preview_msg += f"• ...and {len(channel_names) - 10} more\n"

            await ctx.followup.send(preview_msg, ephemeral=True)

            if dry_run:
                await ctx.followup.send(
                    "✅ Dry run complete. Use `dry_run: False` to actually complete/delete.",
                    ephemeral=True
                )
                return

            # Actual completion and deletion (not dry run)
            await ctx.followup.send(
                f"⚠️ **Starting completion of {total_sessions} drafts and deletion of {total_channels} channels...**",
                ephemeral=True
            )

            # Perform completion and cleanup
            deleted_channels = 0
            deleted_sessions = 0
            completed_drafts = 0
            created_matches = 0
            preserved_matches = 0
            errors = []

            for session in old_sessions:
                try:
                    # Step 1: Create missing matches as draws
                    missing_count = await self._create_missing_matches_as_draws(db_session, session)
                    created_matches += missing_count
                    if missing_count > 0:
                        logger.info(f"Created {missing_count} missing matches as draws for session {session.session_id}")

                    # Step 2: Count unplayed matches for logging
                    unplayed_count = await self._get_unplayed_match_count(db_session, session.session_id)
                    preserved_matches += unplayed_count
                    if unplayed_count > 0:
                        logger.info(f"Preserving {unplayed_count} unplayed matches as draws for session {session.session_id}")

                    # Step 3: Mark draft as completed (if not already)
                    if session.session_stage != 'completed':
                        session.session_stage = 'completed'
                        session.deletion_time = datetime.now()  # Set to now for immediate cleanup
                        completed_drafts += 1
                        logger.info(f"Marked session {session.session_id} as completed")

                    # Step 4: Delete Discord channels
                    if session.channel_ids:
                        for channel_id in session.channel_ids:
                            channel = ctx.guild.get_channel(int(channel_id))
                            if channel:
                                try:
                                    await channel.delete(reason=f"Stale draft cleanup - session older than {hours_old}h")
                                    deleted_channels += 1
                                    await asyncio.sleep(0.5)  # Rate limiting
                                except discord.NotFound:
                                    pass  # Already deleted
                                except discord.HTTPException as e:
                                    errors.append(f"Failed to delete channel {channel.name}: {e}")

                    # Step 5: Delete the draft message if it exists
                    if session.draft_channel_id and session.message_id:
                        try:
                            draft_channel = ctx.guild.get_channel(int(session.draft_channel_id))
                            if draft_channel:
                                message = await draft_channel.fetch_message(int(session.message_id))
                                await message.delete()
                        except (discord.NotFound, discord.HTTPException):
                            pass  # Message already gone

                    # Step 6: Detach MatchResult records before deleting session
                    # Set session_id to NULL to preserve match records
                    from sqlalchemy import update
                    stmt = update(MatchResult).where(
                        MatchResult.session_id == session.session_id
                    ).values(session_id=None)
                    await db_session.execute(stmt)
                    await db_session.flush()

                    # Step 7: Delete from database
                    await db_session.delete(session)
                    deleted_sessions += 1

                except Exception as e:
                    errors.append(f"Error cleaning session {session.session_id}: {e}")
                    logger.error(f"Error during cleanup of session {session.session_id}: {e}")

            # Commit all changes
            await db_session.commit()

            # Send summary
            summary = (
                f"✅ **Cleanup Complete**\n\n"
                f"• Completed {completed_drafts} draft(s) (marked as completed)\n"
                f"• Deleted {deleted_sessions} draft session(s)\n"
                f"• Deleted {deleted_channels} channel(s)\n"
            )

            if created_matches > 0:
                summary += f"• Created {created_matches} missing match(es) as draws\n"

            if preserved_matches > 0:
                summary += f"• Preserved {preserved_matches} unplayed match(es) as draws\n"

            if errors:
                summary += f"\n⚠️ **Errors:** {len(errors)}\n"
                for error in errors[:5]:  # Show first 5 errors
                    summary += f"• {error}\n"
                if len(errors) > 5:
                    summary += f"• ...and {len(errors) - 5} more (check logs)\n"

            await ctx.followup.send(summary, ephemeral=True)
            logger.info(
                f"Cleanup complete: {completed_drafts} drafts completed, "
                f"{deleted_sessions} sessions deleted, {deleted_channels} channels deleted, "
                f"{created_matches} matches created, {preserved_matches} matches preserved as draws"
            )

    @discord.slash_command(
        name='setup_ring_bearer',
        description='Create the ring bearer role for the streak leaderboard system'
    )
    @has_bot_manager_role()
    async def setup_ring_bearer(self, ctx):
        """Create the Discord role needed for the ring bearer system"""
        await ctx.defer(ephemeral=True)

        config = get_config(ctx.guild.id)
        rb_config = config.get("ring_bearer", {})
        role_name = rb_config.get("role_name", "ring bearer")

        # Check if server supports role icons (boost level 2+)
        supports_icons = ctx.guild.premium_tier >= 2

        # Prepare role properties
        role_kwargs = {
            "color": discord.Color.from_rgb(218, 165, 32),  # Gold color
            "hoist": True,  # Show separately in member list
            "mentionable": False,
        }

        # Try to use custom image for role icon if available
        image_used = False
        if supports_icons:
            image_path = Path("images") / "jewel.png"
            if image_path.exists():
                try:
                    with open(image_path, 'rb') as image_file:
                        image_data = image_file.read()
                        role_kwargs["icon"] = image_data
                        image_used = True
                        logger.debug(f"Using custom image for role '{role_name}' from {image_path}")
                except Exception as e:
                    logger.warning(f"Failed to read image {image_path}: {e}")

        # Check if role already exists
        existing_role = discord.utils.get(ctx.guild.roles, name=role_name)

        if existing_role:
            # Role exists - update its properties
            try:
                edit_kwargs = {
                    "color": role_kwargs["color"],
                    "hoist": role_kwargs["hoist"],
                    "mentionable": role_kwargs["mentionable"],
                    "reason": f"Ring bearer role updated by {ctx.author.name}"
                }
                if "icon" in role_kwargs:
                    edit_kwargs["icon"] = role_kwargs["icon"]

                await existing_role.edit(**edit_kwargs)
                logger.info(f"Updated ring bearer role '{role_name}' in {ctx.guild.name}")

                response = f"🔄 **Updated role**: {role_name}"
                if image_used:
                    response += " (with custom icon)"
            except discord.HTTPException as e:
                logger.error(f"Failed to update role '{role_name}': {e}")
                response = f"⚠️ **Error updating role**: {e}"
        else:
            # Role doesn't exist - create it
            try:
                role_kwargs["name"] = role_name
                role_kwargs["reason"] = f"Ring bearer role created by {ctx.author.name}"
                await ctx.guild.create_role(**role_kwargs)
                logger.info(f"Created ring bearer role '{role_name}' in {ctx.guild.name}")

                response = f"✅ **Created role**: {role_name}"
                if image_used:
                    response += " (with custom icon)"
            except discord.HTTPException as e:
                logger.error(f"Failed to create role '{role_name}': {e}")
                response = f"⚠️ **Error creating role**: {e}"

        # Add boost level note
        if not supports_icons:
            response += "\n\n💡 **Tip**: Boost to level 2+ to enable role icons!"

        # Check if ring bearer is enabled
        if not rb_config.get("enabled", False):
            response += "\n\n⚠️ Ring bearer system is not yet enabled. Use `/enable_ring_bearer` to enable it."

        await ctx.followup.send(response, ephemeral=True)

    @discord.slash_command(
        name='enable_ring_bearer',
        description='Enable or disable the ring bearer system for this server'
    )
    @has_bot_manager_role()
    async def enable_ring_bearer(self, ctx, enabled: bool):
        """Enable or disable ring bearer for this guild"""
        from config import get_config, save_config

        await ctx.defer(ephemeral=True)

        config = get_config(ctx.guild.id)

        # Update config
        if "ring_bearer" not in config:
            config["ring_bearer"] = {
                "enabled": enabled,
                "role_name": "ring bearer",
                "icon": "<:coveted_jewel:1460802711694999613>",
                "streak_categories": [
                    "longest_win_streak",
                    "perfect_streak",
                    "draft_win_streak"
                ]
            }
        else:
            config["ring_bearer"]["enabled"] = enabled

        save_config(ctx.guild.id, config)

        status = "enabled" if enabled else "disabled"
        response = f"✅ Ring bearer system has been **{status}** for this server."

        if enabled:
            response += (
                "\n\n**Next steps:**"
                "\n1. Use `/setup_ring_bearer` to create the role if you haven't already"
                "\n2. The ring bearer will be automatically assigned based on streak leaderboards"
                "\n3. Players can claim it by defeating the current ring bearer in matches"
            )
        else:
            response += "\n\nThe ring bearer role will no longer be automatically managed."

        await ctx.followup.send(response, ephemeral=True)
        logger.info(f"Ring bearer {status} for guild {ctx.guild.id} by {ctx.author.name}")

    @discord.slash_command(
        name='refresh_ring_bearer',
        description='Manually refresh the ring bearer based on current leaderboard standings'
    )
    @has_bot_manager_role()
    async def refresh_ring_bearer(self, ctx):
        """Manually trigger a ring bearer refresh"""
        from config import get_config
        from services.ring_bearer_service import update_ring_bearer_for_guild

        await ctx.defer(ephemeral=True)

        config = get_config(ctx.guild.id)
        rb_config = config.get("ring_bearer", {})

        if not rb_config.get("enabled", False):
            await ctx.followup.send(
                "⚠️ Ring bearer system is not enabled. Use `/enable_ring_bearer` to enable it first.",
                ephemeral=True
            )
            return

        try:
            # Manual refresh - no session_id or streak_extensions
            await update_ring_bearer_for_guild(self.bot, str(ctx.guild.id), session_id=None, streak_extensions=None)
            await ctx.followup.send("✅ Ring bearer has been refreshed!", ephemeral=True)
            logger.info(f"Ring bearer manually refreshed for guild {ctx.guild.id} by {ctx.author.name}")
        except Exception as e:
            logger.error(f"Error refreshing ring bearer for guild {ctx.guild.id}: {e}")
            await ctx.followup.send(f"⚠️ Error refreshing ring bearer: {e}", ephemeral=True)


    @discord.slash_command(
        name="admin-stats",
        description="[Admin] View draft statistics for any player"
    )
    @discord.option(
        "player",
        discord.Member,
        description="The player to view stats for",
        required=True
    )
    @discord.option(
        "visibility",
        str,
        description="Who can see the stats?",
        choices=["Just me", "Everyone"],
        default="Just me"
    )
    @has_bot_manager_role()
    async def admin_stats(self, ctx, player: discord.Member, visibility: str = "Just me"):
        """[Admin] View draft statistics for any player."""
        hidden_message = visibility == "Just me"
        await ctx.defer(ephemeral=hidden_message)

        player_id = str(player.id)
        guild_id = str(ctx.guild.id)
        display_name = player.display_name

        try:
            embed = await get_stats_embed_for_player(self.bot, player_id, guild_id, display_name)

            # Add admin indicator to embed footer
            current_footer = embed.footer.text if embed.footer else "Stats are updated after each draft"
            embed.set_footer(text=f"{current_footer} | Requested by {ctx.author.display_name} (Admin)")

            await ctx.followup.send(embed=embed, ephemeral=hidden_message)
        except Exception as e:
            logger.error(f"Error in admin-stats command: {e}")
            await ctx.followup.send(
                f"An error occurred while fetching stats for {player.display_name}. Please try again later.",
                ephemeral=True
            )


def setup(bot):
    bot.add_cog(AdminCommands(bot)) 