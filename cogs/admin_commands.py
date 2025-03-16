from discord.ext import commands
import os
import discord
from loguru import logger

ADMIN_ROLE_NAME = "Bot Manager"

class AdminCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def has_bot_manager_role():
        async def predicate(ctx):
            # Check if user is the owner OR has the admin role
            if await ctx.bot.is_owner(ctx.author):
                return True
            return any(role.name == ADMIN_ROLE_NAME for role in ctx.author.roles)
        return commands.check(predicate)

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

def setup(bot):
    bot.add_cog(AdminCommands(bot)) 