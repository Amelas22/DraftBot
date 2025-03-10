import discord
import aiocron
import pytz
import asyncio
from session import  Team, AsyncSessionLocal, Match, WeeklyLimit, DraftSession
from sqlalchemy import select, not_
from datetime import datetime, timedelta
from collections import Counter
from player_stats import get_player_statistics, create_stats_embed
from loguru import logger
from discord.ext import commands

pacific_time_zone = pytz.timezone('America/Los_Angeles')
cutoff_datetime = pacific_time_zone.localize(datetime(2024, 5, 6, 0, 0))
league_start_time = pacific_time_zone.localize(datetime(2024, 5, 20, 0, 0))

async def league_commands(bot):

    # @bot.slash_command(name="teamfinder", description="Create team finder posts for different regions")
    # async def teamfinder(ctx: discord.ApplicationContext):
    #     await ctx.defer()
    #     from teamfinder import TIMEZONES_AMERICAS, TIMEZONES_EUROPE, TIMEZONES_ASIA_AUSTRALIA, create_view
        
    #     regions = {
    #         "Americas": TIMEZONES_AMERICAS,
    #         "Europe": TIMEZONES_EUROPE,
    #         "Asia/Australia": TIMEZONES_ASIA_AUSTRALIA
    #     }

    #     async with AsyncSessionLocal() as session:
    #         async with session.begin():
    #             for region, timezones in regions.items():
    #                 embed = discord.Embed(title=region, color=discord.Color.blue())
    #                 for label, _ in timezones:
    #                     embed.add_field(name=label, value="No Sign-ups yet", inline=False)

    #                 message = await ctx.send(embed=embed, view=create_view(timezones, ""))
                    
    #                 # Update the message ID in the view
    #                 view = create_view(timezones, str(message.id))
    #                 await message.edit(view=view)

    #                 # Save the message ID, channel ID, and guild ID
    #                 new_record = TeamFinder(
    #                     user_id="system",  # Placeholder for system-generated record
    #                     display_name=f"{region} Post",
    #                     timezone="system",
    #                     message_id=str(message.id),
    #                     channel_id=str(ctx.channel.id),
    #                     guild_id=str(ctx.guild.id)
    #                 )
    #                 session.add(new_record)

    #             await session.commit()
    #     await ctx.followup.send("Click your timezone below to add your name to that timezone. You can click any name to open a DM with that user to coordiante finding teammates. Clicking the timezone again (once signed up) will remove your name from the list.", ephemeral=True)
    @bot.slash_command(name="setup", description="Configure your server with an interactive setup wizard")
    @commands.has_permissions(administrator=True)
    async def setup_wizard(ctx):
        """Start an interactive server setup wizard"""
        await ctx.respond("Starting server setup wizard...", ephemeral=True)
        
        # Launch first step of the wizard
        view = SetupWelcomeView()
        await ctx.followup.send(
            "# Welcome to the Draft Bot Setup Wizard!\n\n"
            "This wizard will help you configure your server for drafting.\n"
            "You'll be asked a series of questions about which features you want to enable.\n\n"
            "At the end, you'll see a summary of all changes before anything is created.",
            view=view,
            ephemeral=True
        )

    @bot.slash_command(name="configure", description="Configure bot settings with an easy interface")
    @commands.has_permissions(administrator=True)
    async def configure_ui(ctx):
        """Interactive configuration system with dropdowns and modals"""
        from config import get_config
        
        # Get the current config
        config = get_config(ctx.guild.id)
        
        # Create the initial category selection view
        view = ConfigCategorySelector(config)
        await ctx.respond("Select a configuration category to modify:", view=view, ephemeral=True)

                
    @bot.slash_command(name="stats", description="Display your draft statistics")
    @discord.option(
        "visibility",
        description="Who can see the stats?",
        required=False,
        choices=["Just me", "Everyone"],
        default="Just me"
    )
    async def stats(ctx, visibility: str = "Just me"):
        """Display your personal draft statistics."""
        # Convert choice to boolean for internal logic
        hidden_message = visibility == "Just me"
        
        # Only defer publicly if stats are meant to be public
        await ctx.defer(ephemeral=hidden_message)
        
        user = ctx.author
        user_id = str(user.id)
        user_display_name = user.display_name
        guild_id = str(ctx.guild.id)  # Get current guild ID
        
        try:
            # Pass guild_id to get_player_statistics
            stats_weekly = await get_player_statistics(user_id, 'week', user_display_name, guild_id)
            stats_monthly = await get_player_statistics(user_id, 'month', user_display_name, guild_id)
            stats_lifetime = await get_player_statistics(user_id, None, user_display_name, guild_id)
            
            # Create and send the embed
            embed = await create_stats_embed(user, stats_weekly, stats_monthly, stats_lifetime)
            await ctx.followup.send(embed=embed, ephemeral=hidden_message)
        except Exception as e:
            logger.error(f"Error in stats command: {e}")
            await ctx.followup.send("An error occurred while fetching your stats. Please try again later.", ephemeral=True)
            
    # @bot.slash_command(name="record", description="Display your head-to-head record against another player")
    # @discord.option("opponent_name", description="The display name of the opponent", required=True)
    # async def record(ctx, opponent_name: str):
    #     """Display your head-to-head record against another player."""
    #     await ctx.defer()
        
    #     user = ctx.author
    #     user_id = str(user.id)
    #     user_display_name = user.display_name
    #     guild_id = str(ctx.guild.id)  # Get current guild ID
        
    #     try:
    #         # Import needed functions from player_stats
    #         from player_stats import find_discord_id_by_display_name, get_head_to_head_stats
            
    #         # Pass guild_id to find_discord_id_by_display_name
    #         opponent_id, opponent_display_name = await find_discord_id_by_display_name(opponent_name, guild_id)
            
    #         if not opponent_id:
    #             await ctx.followup.send(f"Could not find a player with the display name '{opponent_name}' in this server.", ephemeral=True)
    #             return
            
    #         # Pass guild_id to get_head_to_head_stats
    #         h2h_stats = await get_head_to_head_stats(user_id, opponent_id, user_display_name, opponent_display_name, guild_id)
            
    #         # Rest of the function...
    #     except Exception as e:
    #         logger.error(f"Error in record command: {e}")
    #         await ctx.followup.send("An error occurred while fetching the record. Please try again later.", ephemeral=True)
                    
    # @bot.slash_command(name="registerteam", description="Register a new team in the league")
    # async def register_team(interaction: discord.Interaction, team_name: str):
    #     cube_overseer_role_name = "Cube Overseer"
    #     if cube_overseer_role_name not in [role.name for role in interaction.user.roles]:
    #         await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
    #         return
        
    #     team, response_message = await register_team_to_db(team_name)
    #     await interaction.response.send_message(response_message, ephemeral=True)
    
    # @bot.slash_command(name="delete_team", description="Mod Only: Remove a new team from the league")
    # async def deleteteam(ctx, *, team_name: str):
    #     await ctx.defer()  # Acknowledge the interaction immediately to prevent timeout
    #     response_message = await remove_team_from_db(ctx, team_name)
    #     await ctx.followup.send(response_message)

    # @bot.slash_command(name='list_teams', description='List all registered teams')
    # async def list_teams(interaction: discord.Interaction):
    #     async with AsyncSessionLocal() as session:
    #         async with session.begin():
    #             # Fetch all teams sorted by their name
    #             stmt = select(Team).order_by(Team.TeamName.asc())
    #             result = await session.execute(stmt)
    #             teams = result.scalars().all()

    #         # If there are no teams registered
    #         if not teams:
    #             await interaction.response.send_message("No teams have been registered yet.", ephemeral=True)
    #             return

    #         # Create an embed to list all teams
    #         embed = discord.Embed(title="Registered Teams", description="", color=discord.Color.blue())
            
    #         # Adding each team to the embed description
    #         for team in teams:
    #             embed.description += f"- {team.TeamName}\n"

    #         await interaction.response.send_message(embed=embed)

    @bot.slash_command(name='winston_draft', description='Lists all available slash commands')
    async def winstondraft(interaction: discord.Interaction):
        from config import is_special_guild, get_config
    
        # Only allow this command in your guild
        if not is_special_guild(interaction.guild_id):
            await interaction.response.send_message("Winston draft is not available on this server.", ephemeral=True)
            return
        from utils import create_winston_draft
        await create_winston_draft(bot, interaction)
        await interaction.response.send_message("Queue posted in #winston-draft. Good luck!", ephemeral=True)
    # @bot.slash_command(name='commands', description='Lists all available slash commands')
    # async def list_commands(ctx):
    #     # Manually creating a list of commands and descriptions
    #     commands_list = {
    #         "`/commands`": "Lists all available slash commands.\n",
    #         "**Lobby Commands**" : "",
    #         "**`/startdraft`**": "Launch a lobby for randomized team drafts.",
    #         "**`/leaguedraft`**": "Launch a lobby for League Drafts (results tracked)",
    #         "**`/premadedraft`**": "Launch a lobby for premade teams (untracked)\n",
    #         "**League Commands**": "",
    #         "**`/post_challenge`**": "Set a draft time for other teams to challenge your team.",
    #         "**`/list_challenges`**": "Lists all open challenges with a link to sign up.",
    #         "**`/list_teams`**": "Displays registered teams",
    #         "**`/find_a_match`**": "Choose a time to find challenges within 2 hours of chosen time.",
    #         "**`/standings`**": "Displays current league standings\n",
    #         "**Open Queue Commands**": "",
    #         "**`/trophies`**": "Displays this month's trophy leaderboard",
    #         "**Mod Commands**": "",
    #         "**`/delete_team`**": "Removes a registered team",
    #         "**`/registerteam`**": "Register your team for the league",
    #         "**`/register_player`**": "Register a player to a team",
    #         "**`/remove_player`**": "Removes a player from all teams",
    #     }
        
    #     # Formatting the list for display
    #     commands_description = "\n".join([f"{cmd}: {desc}" for cmd, desc in commands_list.items()])
        
    #     # Creating an embed to nicely format the list of commands
    #     embed = discord.Embed(title="Available Commands", description=commands_description, color=discord.Color.blue())
        
    #     await ctx.respond(embed=embed)

    # @bot.slash_command(name="swiss_scheduled_draft", description="Schedule a forthcoming draft")
    # async def scheduledraft(interaction: discord.Interaction):
    #     await interaction.response.defer(ephemeral=True)
    #     from league import InitialPostView
    #     initial_view = InitialPostView(command_type="swiss")
    #     await interaction.followup.send(f"Post a scheduled draft. Select Cube and Timezone.", view=initial_view, ephemeral=True)

    # @bot.slash_command(name="post_challenge", description="Post a challenge for your team")
    # async def postchallenge(interaction: discord.Interaction):
    #     global cutoff_datetime

    #     # Check if current time is before the cutoff time
    #     current_time = datetime.now(pacific_time_zone)
    #     if current_time >= cutoff_datetime:
    #         await interaction.response.send_message("This season is no longer active. Keep an eye on announcements for future seasons!", ephemeral=True)
    #         return
        
    #     await interaction.response.defer(ephemeral=True)
        
    #     user_id_str = str(interaction.user.id)
        
    #     try:
    #         async with AsyncSessionLocal() as session:  # Assuming AsyncSessionLocal is your session maker
    #             async with session.begin():
    #                 # Query for any team registration entries that include the user ID in their TeamMembers
    #                 stmt = select(TeamRegistration).where(TeamRegistration.TeamMembers.contains(user_id_str))
    #                 result = await session.execute(stmt)
    #                 team_registration = result.scalars().first()

    #                 if team_registration:
    #                     # Extracting user details
    #                     team_id = team_registration.TeamID
    #                     team_name = team_registration.TeamName
    #                     user_display_name = team_registration.TeamMembers.get(user_id_str)
    #                     from league import InitialPostView
    #                     initial_view = InitialPostView(command_type="post", team_id=team_id, team_name=team_name, user_display_name=user_display_name)
    #                     await interaction.followup.send(f"Post a Challenge for {team_name}. Select Cube and Timezone.", view=initial_view, ephemeral=True)
    #                 else:
    #                     await interaction.followup.send(f"You are not registered to a team. Contact a Cube Overseer if this is an error.", ephemeral=True)
    #     except Exception as e:
    #         await interaction.followup.send(f"An error occurred while processing your request: {str(e)}", ephemeral=True)
    #         print(f"Error in postchallenge command: {e}")  

    @bot.slash_command(name="schedule_test_draft", description="Post a scheduled draft")
    async def scheduledraft(interaction: discord.Interaction):
        guild = interaction.guild_id
        if guild != 336345350535118849:
            from league import InitialPostView
            initial_view = InitialPostView(command_type="test", team_id=1)
            await interaction.response.send_message(f"Post a scheduled draft. Select a Timezone to start.", view=initial_view, ephemeral=True)
        else:
            await interaction.response.send_message("This command is only usable on the test server.")

    # @bot.slash_command(name="schedule_draft", description="Post a scheduled draft")
    # async def schedule_draft(interaction: discord.Interaction):
    #     from modals import CubeSelectionModal
    #     await interaction.response.send_modal(CubeSelectionModal(session_type="schedule", title="Select Cube"))
        
    # @bot.slash_command(
    # name="remove_user_from_team",
    # description="Remove a user from all teams they are assigned to"
    # )
    # @discord.option(
    #     "user_id",
    #     description="The Discord user ID of the member to remove from teams",
    #     required=True
    # )
    # async def remove_user_from_team(interaction: discord.Interaction, user_id: str):
    #     # Check if the user has the "Cube Overseer" role
    #     cube_overseer_role_name = "Cube Overseer"
    #     if cube_overseer_role_name not in [role.name for role in interaction.user.roles]:
    #         await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
    #         return

    #     async with AsyncSessionLocal() as session:
    #         async with session.begin():
    #             # Convert user_id to str if not already to ensure consistency in comparison
    #             user_id_str = str(user_id)
    #             # Query for any team registration entries that include the user ID in their TeamMembers
    #             stmt = select(TeamRegistration)
    #             all_team_registrations = await session.execute(stmt)
    #             teams_updated = 0

    #             for team_registration in all_team_registrations.scalars().all():
    #                 if user_id_str in team_registration.TeamMembers:
    #                     # Remove the user from the TeamMembers dictionary
    #                     print(team_registration.TeamMembers[user_id_str])
    #                     del team_registration.TeamMembers[user_id_str]
    #                     flag_modified(team_registration, "TeamMembers")
    #                     session.add(team_registration)
    #                     teams_updated += 1

    #             await session.commit()

    #     if teams_updated > 0:
    #         await interaction.response.send_message(f"User {user_id} was successfully removed from {teams_updated} teams.", ephemeral=True)
    #     else:
    #         await interaction.response.send_message(f"User {user_id} was not found in any teams.", ephemeral=True)


    # @bot.slash_command(name="register_player", description="Post a challenge for your team")
    # async def registerplayer(interaction: discord.Interaction):
    #     cube_overseer_role = discord.utils.get(interaction.guild.roles, name="Cube Overseer")
    
    #     if cube_overseer_role in interaction.user.roles:
    #         from league import InitialPostView
    #         initial_view = InitialPostView(command_type="register")
    #         await interaction.response.send_message("Please select the range for the team", view=initial_view, ephemeral=True)
    #     else:
    #         # Responding with a message indicating lack of permission
    #         await interaction.response.send_message("You do not have permission to register players, please tag Cube Overseer if you need to make changes.", ephemeral=True)

    
            
    # @bot.slash_command(name="find_a_match", description="Find an open challenge based on a given time.")
    # async def findamatch(interaction: discord.Interaction):
    #     global cutoff_datetime

    #     # Check if current time is before the cutoff time
    #     current_time = datetime.now(pacific_time_zone)
    #     if current_time >= cutoff_datetime:
    #         await interaction.response.send_message("This season is no longer active. Keep an eye on announcements for future seasons!", ephemeral=True)
    #         return            
    #     from league import InitialPostView
    #     initial_view = InitialPostView(command_type="find")
    #     await interaction.response.send_message("Please select the range for your team", view=initial_view, ephemeral=True)

    # @bot.slash_command(name="list_scheduled_drafts", description="List all open scheduled drafts in chronological order.")
    # async def listscheduledswiss(interaction: discord.Interaction):
    #     now = datetime.now()
    #     async with AsyncSessionLocal() as db_session: 
    #         async with db_session.begin():
    #             from session import SwissChallenge
    #             stmt = select(SwissChallenge).where(SwissChallenge.start_time > now
    #                                             ).order_by(SwissChallenge.start_time.asc())
    #             results = await db_session.execute(stmt)
    #             scheduled_drafts = results.scalars().all()

    #             if not scheduled_drafts:
    #             # No challenges found within the range
    #                 await interaction.response.send_message("No scheduled drafts. Use /swiss_scheduled_draft to open a scheduled draft or /swiss_draft to open an on demand draft", ephemeral=True)
    #                 return

    #             embed = discord.Embed(title="Currently Scheduled Drafts", description="", color=discord.Color.blue())
    #             for draft in scheduled_drafts:
    #                 message_link = f"https://discord.com/channels/{draft.guild_id}/{draft.channel_id}/{draft.message_id}"
    #                 start_time = draft.start_time
    #                 num_sign_ups = len(draft.sign_ups)
    #                 formatted_time = f"<t:{int(start_time.timestamp())}:F>"
    #                 relative_time = f"<t:{int(start_time.timestamp())}:R>"
    #                 embed.add_field(name=f"Draft Scheduled: {formatted_time} ({relative_time})", value=f"Cube: {draft.cube}\nCurrent Signups: {num_sign_ups} \n[Sign Up Here!]({message_link})", inline=False)
    #             await interaction.response.send_message(embed=embed)

    # @bot.slash_command(name="list_challenges", description="List all open challenges in chronological order.")
    # async def list_challenge(interaction: discord.Interaction):
    #     global cutoff_datetime

    #     # Check if current time is before the cutoff time
    #     current_time = datetime.now(pacific_time_zone)
    #     if current_time >= cutoff_datetime:
    #         await interaction.response.send_message("This season is no longer active. Keep an eye on announcements for future seasons!", ephemeral=True)
    #         return
        
    #     async with AsyncSessionLocal() as db_session: 
    #         async with db_session.begin():
    #             from session import Challenge
    #             range_stmt = select(Challenge).where(Challenge.team_b == None,
    #                                                 Challenge.message_id != None
    #                                                 ).order_by(Challenge.start_time.asc())
                                                
    #             results = await db_session.execute(range_stmt)
    #             challenges = results.scalars().all()

    #             if not challenges:
    #             # No challenges found within the range
    #                 await interaction.response.send_message("No open challenges. Consider using /post_challenge to open a challenge yourself!", ephemeral=True)
    #                 return
    #             # Construct the link to the original challenge message
                
    #             embed = discord.Embed(title="Open Challenges", description="Here are all open challenges", color=discord.Color.blue())

    #             for challenge in challenges:
    #                 message_link = f"https://discord.com/channels/{challenge.guild_id}/{challenge.channel_id}/{challenge.message_id}"
    #                 # Mention the initial user who posted the challenge
    #                 initial_user_mention = f"<@{challenge.initial_user}>"
    #                 # Format the start time of each challenge to display in the embed
    #                 start_time = challenge.start_time
    #                 formatted_time = f"<t:{int(start_time.timestamp())}:F>"
    #                 relative_time = f"<t:{int(start_time.timestamp())}:R>"
    #                 embed.add_field(name=f"Team: {challenge.team_a}", value=f"Time: {formatted_time} ({relative_time})\nCube: {challenge.cube}\nPosted by: {initial_user_mention}\n[Sign Up Here!]({message_link})", inline=False)
    #             await interaction.response.send_message(embed=embed)


    @bot.event
    async def on_reaction_add(reaction, user):
        from config import get_config, is_special_guild
        
        # Only run bot detection in your guild
        if not is_special_guild(reaction.message.guild.id):
            return
        
        config = get_config(reaction.message.guild.id)
        role_request_channel = config["channels"].get("role_request")
        
        # Check if the reaction is in the role-request channel
        if reaction.message.channel.name == role_request_channel:
            # Ensure user is a Member object
            if reaction.message.guild:
                member = await reaction.message.guild.fetch_member(user.id)
                # Check if the user has no roles other than @everyone
                if len(member.roles) == 1:
                    # Find the 'suspected bot' role in the guild
                    suspected_bot_role_name = config["roles"].get("suspected_bot")
                    suspected_bot_role = discord.utils.get(member.guild.roles, name=suspected_bot_role_name)
                    if suspected_bot_role:
                        try:
                            await member.add_roles(suspected_bot_role)
                            print(f"Assigned '{suspected_bot_role_name}' role to {member.name}")
                        except discord.Forbidden:
                            print(f"Permission error: Unable to assign roles. Check the bot's role position and permissions.")
                        except discord.HTTPException as e:
                            print(f"HTTP exception occurred: {e}")



    # @bot.slash_command(name='standings', description='Display the team standings by points earned')
    # async def standings(interaction: discord.Interaction):
    #     global cutoff_datetime

    #     # Check if current time is before the cutoff time
    #     current_time = datetime.now(pacific_time_zone)
    #     if current_time >= cutoff_datetime:
    #         await interaction.response.send_message("This season is no longer active. Keep an eye on announcements for future seasons!", ephemeral=True)
    #         return
        
    #     await post_standings(interaction)

    # @bot.slash_command(name="trophies", description="Display the Trophy Leaderboard for the current month.")
    # async def trophies(ctx):
    #     eastern_tz = pytz.timezone('US/Eastern')
    #     now = datetime.now(eastern_tz)
    #     first_day_of_month = eastern_tz.localize(datetime(now.year, now.month, 1))

    #     async with AsyncSessionLocal() as db_session:
    #         async with db_session.begin():
    #             stmt = select(DraftSession).where(
    #                 DraftSession.teams_start_time.between(first_day_of_month, now),
    #                 not_(DraftSession.trophy_drafters == None),
    #                 DraftSession.session_type == "random"
    #             )
    #             results = await db_session.execute(stmt)
    #             trophy_sessions = results.scalars().all()

    #             drafter_counts = Counter()
    #             for session in trophy_sessions:
    #                 undefeated_drafters = session.trophy_drafters if session.trophy_drafters else []
    #                 drafter_counts.update(undefeated_drafters)

    #             # Get only the drafters with more than one trophy
    #             sorted_drafters = [drafter for drafter in drafter_counts.items() if drafter[1] > 1]

    #             # Now sort and take only the top 25
    #             sorted_drafters = sorted(sorted_drafters, key=lambda x: x[1], reverse=True)[:25]

    #             embed = discord.Embed(
    #                 title=f"{now.strftime('%B')} Trophy Leaderboard",
    #                 description="Drafters with multiple trophies in Open-Queue",
    #                 color=discord.Color.blue()
    #             )

    #             last_count = None
    #             rank = 0
    #             actual_rank = 0
    #             skip_next_rank = 0

    #             for drafter, count in sorted_drafters:
    #                 if count != last_count:
    #                     rank += 1 + skip_next_rank
    #                     display_rank = str(rank)
    #                     skip_next_rank = 0
    #                 else:
    #                     display_rank = f"T{rank}"  # Tie rank
    #                     skip_next_rank += 1

    #                 last_count = count

    #                 if actual_rank < 25:  # Ensure we don't exceed 25 fields
    #                     rank_title = f"{display_rank}. {drafter}"
    #                     embed.add_field(name=rank_title, value=f"Trophies: {count}", inline=False)
    #                     actual_rank += 1
    #                 else:
    #                     break

    #             await ctx.respond(embed=embed)

    # @bot.slash_command(name="leaguedraft", description="Start a league draft with chosen teams and cube.")
    # async def leaguedraft(interaction: discord.Interaction):
    #     global cutoff_datetime

    #     # Check if current time is before the cutoff time
    #     current_time = datetime.now(pacific_time_zone)
    #     if current_time >= cutoff_datetime:
    #         await interaction.response.send_message("This season is no longer active. Keep an eye on announcements for future seasons!", ephemeral=True)
    #         return
        
    #     from league import InitialRangeView   
    #     initial_view = InitialRangeView()
    #     await interaction.response.send_message("Step 1 of 2: Please select the range for your team and the opposing team:", view=initial_view, ephemeral=True)
        
#     @aiocron.crontab('01 09 * * *', tz=pytz.timezone('US/Eastern'))
#     async def daily_league_results():
#         global cutoff_datetime

#         # Check if current time is before the cutoff time
#         current_time = datetime.now(pacific_time_zone)
#         if current_time >= cutoff_datetime:
#             return      
        
#         # Fetch all guilds the bot is in and look for the "league-summary" channel
#         for guild in bot.guilds:
#             channel = discord.utils.get(guild.text_channels, name="league-summary")
#             if channel:
#                 break  # If we find the channel, we exit the loop
        
#         if not channel:  # If the bot cannot find the channel in any guild, log an error and return
#             print("Error: 'league-summary' channel not found.")
#             return

#         eastern_tz = pytz.timezone('US/Eastern')
#         now = datetime.now(eastern_tz)
#         start_time = eastern_tz.localize(datetime(now.year, now.month, now.day, 3, 0)) - timedelta(days=1)  # 3 AM previous day
#         end_time = start_time + timedelta(hours=24)  # 3 AM current day

#         async with AsyncSessionLocal() as db_session:
#             async with db_session.begin():
#                 stmt = select(Match).where(Match.MatchDate.between(start_time, end_time),
#                                         not_(Match.DraftWinnerID == None))
#                 results = await db_session.execute(stmt)
#                 matches = results.scalars().all()

#                 if not matches:
#                     await channel.send("No matches found in the last 24 hours.")
#                     return
                
#                 trophy_drafter_stmt = select(DraftSession).where(DraftSession.teams_start_time.between(start_time, end_time),
#                                                                  not_(DraftSession.premade_match_id),
#                                                                  DraftSession.tracked_draft==1)
#                 trophy_results = await db_session.execute(trophy_drafter_stmt)
#                 trophy_sessions = trophy_results.scalars().all()

#                 drafter_counts = Counter()
#                 for session in trophy_sessions:
#                     undefeated_drafters = list(session.trophy_drafters) if session.trophy_drafters else []
#                     drafter_counts.update(undefeated_drafters)

#                 undefeated_drafters_field_value = "\n".join([f"{drafter} x{count}" if count > 1 else drafter for drafter, count in drafter_counts.items()])


#                 date_str = start_time.strftime("%B %d, %Y")
#                 embed = discord.Embed(title=f"Daily League Results - {date_str}", description="", color=discord.Color.blue())
#                 for match in matches:
#                     result_line = f"**{match.TeamAName}** defeated **{match.TeamBName}** ({match.TeamAWins} - {match.TeamBWins})" if match.TeamAWins > match.TeamBWins else f"**{match.TeamBName}** defeated **{match.TeamAName}** ({match.TeamBWins} - {match.TeamAWins})"
#                     embed.description += result_line + "\n"
#                 embed.add_field(name="**Trophy Drafters**", value=undefeated_drafters_field_value or "None", inline=False)
#                 await channel.send(embed=embed)

#     @aiocron.crontab('00 13 * * *', tz=pytz.timezone('US/Eastern'))  
#     async def post_todays_matches():
#         global cutoff_datetime

#         # Check if current time is before the cutoff time
#         current_time = datetime.now(pacific_time_zone)
#         if current_time >= cutoff_datetime:
#             return  
        
#         for guild in bot.guilds:
#             channel = discord.utils.get(guild.text_channels, name="league-summary")
#             if channel:
#                 break  # If we find the channel, we exit the loop
        
#         if not channel:  # If the bot cannot find the channel in any guild, log an error and return
#             print("Error: 'league-summary' channel not found.")
#             return
        
#         eastern = pytz.timezone('US/Eastern')
#         now = datetime.now(eastern).replace(hour=13, minute=0, second=0, microsecond=0)
#         tomorrow = now + timedelta(days=1)

#         # Convert times to UTC as your database stores times in UTC
#         now_utc = now.astimezone(pytz.utc)
#         tomorrow_utc = tomorrow.astimezone(pytz.utc)

#         async with AsyncSessionLocal() as session:
#             async with session.begin():
#                 # Scheduled Matches
#                 from session import Challenge
#                 scheduled_stmt = select(Challenge).where(
#                     Challenge.start_time.between(now_utc, tomorrow_utc),
#                     Challenge.team_b_id.isnot(None)
#                 ).order_by(Challenge.start_time.asc())
#                 scheduled_result = await session.execute(scheduled_stmt)
#                 scheduled_matches = scheduled_result.scalars().all()

#                 # Open Challenges
#                 open_stmt = select(Challenge).where(
#                     Challenge.start_time.between(now_utc, tomorrow_utc),
#                     Challenge.team_b_id.is_(None)
#                 ).order_by(Challenge.start_time.asc())
#                 open_result = await session.execute(open_stmt)
#                 open_challenges = open_result.scalars().all()

#                 embed = discord.Embed(title="Today's Matches", color=discord.Color.blue())
#                 # Add fields or descriptions to embed based on scheduled_matches and open_challenges
#                 embed.add_field(name="Scheduled Matches", value="No Matches Scheduled" if not scheduled_matches else "", inline=False)
#                 if scheduled_matches:
#                     sch_count = 1
#                     for match in scheduled_matches:
#                         #print(match.guild_id)
#                         message_link = f"https://discord.com/channels/{match.guild_id}/{match.channel_id}/{match.message_id}"
#                         # Mention the initial user who posted the challenge
#                         initial_user_mention = f"<@{match.initial_user}>"
#                         opponent_user_mention = f"<@{match.opponent_user}>"
#                         # Format the start time of each challenge to display in the embed
#                         time = datetime.strptime(str(match.start_time), "%Y-%m-%d %H:%M:%S")
#                         utc_zone = pytz.timezone("UTC")
#                         start_time = utc_zone.localize(time)
#                         formatted_time = f"<t:{int(start_time.timestamp())}:F>"
#                         relative_time = f"<t:{int(start_time.timestamp())}:R>"
#                         embed.add_field(name=f"{sch_count}. {match.team_a} v. {match.team_b}", value=f"Draft Start Time: {formatted_time} ({relative_time})\nCube: {match.cube}\nTeam Leads: {initial_user_mention} {opponent_user_mention}\n[Challenge Link]({message_link})", inline=False)
#                         sch_count += 1

#                 embed.add_field(name="\n\nOpen Challenges", value="No Open Challenges" if not open_challenges else "", inline=False)
#                 if open_challenges:
#                     open_count = 1
#                     for match in open_challenges:
#                         #print(match.guild_id)
#                         message_link = f"https://discord.com/channels/{match.guild_id}/{match.channel_id}/{match.message_id}"
#                         # Mention the initial user who posted the challenge
#                         initial_user_mention = f"<@{match.initial_user}>"
#                         # Format the start time of each challenge to display in the embed
#                         time = datetime.strptime(str(match.start_time), "%Y-%m-%d %H:%M:%S")
#                         utc_zone = pytz.timezone("UTC")
#                         start_time = utc_zone.localize(time)
#                         formatted_time = f"<t:{int(start_time.timestamp())}:F>"
#                         relative_time = f"<t:{int(start_time.timestamp())}:R>"
#                         embed.add_field(name=f"{open_count}. Team: {match.team_a}", value=f"Proposed Start Time: {formatted_time} ({relative_time})\nCube: {match.cube}\nPosted by: {initial_user_mention}\n[Sign Up Here!]({message_link})", inline=False)
#                         open_count += 1
#                 await channel.send(embed=embed)
#     @aiocron.crontab('00 09 * * *', tz=pytz.timezone('US/Eastern'))
#     async def post_league_standings():
#         global cutoff_datetime

#         # Check if current time is before the cutoff time
#         current_time = datetime.now(pacific_time_zone)
#         if current_time >= cutoff_datetime:
#             return  
        
#         # Fetch all guilds the bot is in and look for the "league-summary" channel
#         for guild in bot.guilds:
#             channel = discord.utils.get(guild.text_channels, name="league-summary")
#             if channel:
#                 break  # If we find the channel, we exit the loop
        
#         if not channel:  # If the bot cannot find the channel in any guild, log an error and return
#             print("Error: 'league-summary' channel not found.")
#             return
        
#         time = datetime.now()
#         count = 1
#         async with AsyncSessionLocal() as session:
#             async with session.begin():
#                 # Fetch teams ordered by PointsEarned (DESC) and MatchesCompleted (ASC)
#                 stmt = (select(Team)
#                     .where(Team.MatchesCompleted >= 1)
#                     .order_by(Team.PointsEarned.desc(), Team.MatchesCompleted.asc(), Team.PreseasonPoints.desc()))
#                 results = await session.execute(stmt)
#                 teams = results.scalars().all()
                
#                 # Check if teams exist
#                 if not teams:
#                     await channel.send("No results posted yet.")
#                     return
#                 embed = discord.Embed(title="Team Standings", description=f"Standings as of <t:{int(time.timestamp())}:F>", color=discord.Color.gold())
#                 last_points = None
#                 last_matches = None
#                 last_preseason = None
#                 actual_rank = 0
#                 display_rank = 0
                
#                 # Iterate through teams to build the ranking
#                 for team in teams:
#                     # Increase actual_rank each loop, this is the absolute position in the list
#                     actual_rank += 1
#                     # Only increase display_rank if the current team's stats do not match the last team's stats
#                     if (team.PointsEarned, team.MatchesCompleted, team.PreseasonPoints) != (last_points, last_matches, last_preseason):
#                         display_rank = actual_rank
#                     last_points = team.PointsEarned
#                     last_matches = team.MatchesCompleted
#                     last_preseason = team.PreseasonPoints

#                     # Check if the rank should be displayed as tied
#                     rank_text = f"T{display_rank}" if actual_rank != display_rank else str(display_rank)
                    
#                     preseason_text = f", Preseason Points: {team.PreseasonPoints}" if team.PreseasonPoints > 0 else ""
#                     embed.add_field(
#                         name=f"{rank_text}. {team.TeamName}", 
#                         value=f"Points Earned: {team.PointsEarned}, Matches Completed: {team.MatchesCompleted}{preseason_text}", 
#                         inline=False
#                     )
                    
#                     # Limit to top 50 teams in two batches
#                     if actual_rank == 25:
#                         await channel.send(embed=embed)
#                         embed = discord.Embed(title="Team Standings, Continued", description="", color=discord.Color.gold())
#                     elif actual_rank == 50:
#                         break

#                 # Send the last batch if it exists
#                 if actual_rank > 25:
#                     await channel.send(embed=embed)
#     @aiocron.crontab('00 10 * * 1', tz=pytz.timezone('US/Eastern'))  # At 10:00 on Monday, Eastern Time
#     async def schedule_weekly_summary():
#         global cutoff_datetime

#         # Check if current time is before the cutoff time
#         current_time = datetime.now(pacific_time_zone)
#         if current_time >= cutoff_datetime:
#             return  
        
#         await weekly_summary(bot)   

# async def swiss_draft_commands(bot):

#     @aiocron.crontab('00 14 * * *', tz=pytz.timezone('US/Eastern'))
#     async def daily_swiss_results():
#         global league_start_time

#         # Check if current time is before the cutoff time
#         current_time = datetime.now(pacific_time_zone)
#         if current_time < league_start_time + timedelta(hours=11):
#             return
        
#         for guild in bot.guilds:
#             channel = discord.utils.get(guild.text_channels, name="league-draft-results")      
#             if not channel:  # If the bot cannot find the channel in any guild, log an error and continue
#                 continue
#             eastern_tz = pytz.timezone('US/Eastern')
#             now = datetime.now(eastern_tz)
#             start_time = eastern_tz.localize(datetime(now.year, now.month, now.day, 3, 0)) - timedelta(days=1)  # 3 AM previous day
#             end_time = start_time + timedelta(hours=24)  # 3 AM current day

#             async with AsyncSessionLocal() as db_session:
#                 async with db_session.begin():
#                     stmt = select(DraftSession).where(DraftSession.teams_start_time.between(start_time, end_time),
#                                             not_(DraftSession.victory_message_id_results_channel == None),
#                                             DraftSession.session_type == "swiss")
#                     results = await db_session.execute(stmt)
#                     matches = results.scalars().all()

#                     if not matches:
#                         await channel.send("No matches found in the last 24 hours.")
#                         return
                    

#                     total_drafts = len(matches)
#                     date_str = start_time.strftime("%B %d, %Y")
#                     embed = discord.Embed(title=f"Daily League Results - {date_str}", description="", color=discord.Color.blue())
#                     embed.add_field(name="**Completed Drafts**", value=total_drafts, inline=False)
#                     from utils import calculate_player_standings
#                     top_15_embeds = await calculate_player_standings(limit=15)

#                     if top_15_embeds:
#                         top_15_standings = top_15_embeds[0].fields[0].value
#                         embed.add_field(name="Top 15 Standings", value=top_15_standings, inline=False)

#                     await channel.send(embed=embed)

#     @bot.slash_command(name="swiss_draft", description="Post an eight player swiss pod")
#     async def swiss(interaction: discord.Interaction):
#         global league_start_time

#         # Check if current time is before the cutoff time
#         current_time = datetime.now(pacific_time_zone)
#         if current_time < league_start_time:
#             await interaction.response.send_message("This season is not yet active. Season begins on Monday, May 20th! Please reach out to a Cube Overseer if you believe you received this message in error.", ephemeral=True)
#             return
        
#         from modals import CubeSelectionModal
#         await interaction.response.send_modal(CubeSelectionModal(session_type="swiss", title="Select Cube"))

#     @bot.slash_command(name='player_standings', description='Display the AlphaFrog standings')
#     async def player_standings(interaction: discord.Interaction):
#         global league_start_time
#         await interaction.response.defer()
#         # Check if current time is before the cutoff time
#         current_time = datetime.now(pacific_time_zone)
#         if current_time < league_start_time:
#             await interaction.response.send_message("This season is not yet active. Season begins on Monday, May 20th! Please reach out to a Cube Overseer if you believe you received this message in error.", ephemeral=True)
#             return
#         from utils import calculate_player_standings
#         embeds = await calculate_player_standings()
#         for embed in embeds:
#             await interaction.followup.send(embed=embed)

async def scheduled_posts(bot):

    @aiocron.crontab('00 10 * * 1', tz=pytz.timezone('US/Eastern'))
    async def weekly_random_results():
        # Fetch all guilds the bot is in and look for the "league-summary" channel
        for guild in bot.guilds:
            channel = discord.utils.get(guild.text_channels, name="team-draft-results")      
            if not channel:  # If the bot cannot find the channel in any guild, log an error and return
                continue

            eastern_tz = pytz.timezone('US/Eastern')
            now = datetime.now(eastern_tz)
            start_time = eastern_tz.localize(datetime(now.year, now.month, now.day, 3, 0)) - timedelta(days=7)  # 3 AM previous day
            end_time = start_time + timedelta(days=7)  # 3 AM current day

            async with AsyncSessionLocal() as db_session:
                async with db_session.begin():
                    # Query for DraftSessions within the time range
                    stmt = select(DraftSession).where(
                        DraftSession.teams_start_time.between(start_time, end_time),
                        not_(DraftSession.victory_message_id_draft_chat == None),
                        DraftSession.session_type == "random",
                        DraftSession.guild_id == str(guild.id)
                    )
                    result = await db_session.execute(stmt)
                    sessions = result.scalars().all()

                    if not sessions:
                        await channel.send("No matches found in the last 24 hours.")
                        continue
                    
                    all_usernames = []
                    for session in sessions:
                        # Directly use the sign_ups dictionary
                        usernames = list(session.sign_ups.values())
                        all_usernames.extend(usernames)

                    username_counts = Counter(all_usernames)
                    top_drafters = username_counts.most_common(10)


                    drafter_counts = Counter()
                    for session in sessions:
                        if session.trophy_drafters:
                            drafter_counts.update(session.trophy_drafters)

                    # Filter and sort drafters who have two or more trophies
                    filtered_trophy_drafters = {drafter: count for drafter, count in drafter_counts.items() if count >= 2}
                    sorted_trophy_drafters = sorted(filtered_trophy_drafters.items(), key=lambda item: item[1], reverse=True)

                    # Format the drafter names and their counts for display
                    if sorted_trophy_drafters:
                        undefeated_drafters_field_value = "\n".join([f"{index + 1}. {drafter} x{count}" for index, (drafter, count) in enumerate(sorted_trophy_drafters)])
                    else:
                        undefeated_drafters_field_value = "No drafters with 2 or more trophies."

                    total_drafts = len(sessions)

                    date_str = end_time.strftime("%B %d, %Y")
                    top_drafters_field_value = "\n".join([f"{index + 1}. **{name}:** {count} drafts" for index, (name, count) in enumerate(top_drafters)])
                    embed = discord.Embed(title=f"Open Queue Weekly Summary - Week Ending {date_str}", description="", color=discord.Color.magenta())
                    embed.add_field(name="**Completed Drafts**", value=total_drafts, inline=False)
                    embed.add_field(name="**Top 10 Drafters**\n", value=top_drafters_field_value, inline=False)
                    embed.add_field(name="**Multiple Weekly Trophies**", value=undefeated_drafters_field_value or "No trophies :(", inline=False)

                    await channel.send(embed=embed)

    @aiocron.crontab('15 09 * * *', tz=pytz.timezone('US/Eastern'))
    async def daily_random_results():
        # Fetch all guilds the bot is in and look for the "league-summary" channel
        for guild in bot.guilds:
            channel = discord.utils.get(guild.text_channels, name="team-draft-results")      
            if not channel:  # If the bot cannot find the channel in any guild, log an error and return
                continue

            eastern_tz = pytz.timezone('US/Eastern')
            now = datetime.now(eastern_tz)
            start_time = eastern_tz.localize(datetime(now.year, now.month, now.day, 3, 0)) - timedelta(days=1)  # 3 AM previous day
            end_time = start_time + timedelta(hours=24)  # 3 AM current day

            async with AsyncSessionLocal() as db_session:
                async with db_session.begin():
                    # Query for DraftSessions within the time range
                    stmt = select(DraftSession).where(
                        DraftSession.teams_start_time.between(start_time, end_time),
                        not_(DraftSession.victory_message_id_draft_chat == None),
                        DraftSession.session_type == "random",
                        DraftSession.guild_id == str(guild.id)
                    )
                    result = await db_session.execute(stmt)
                    sessions = result.scalars().all()

                    if not sessions:
                        await channel.send("No matches found in the last 24 hours.")
                        continue
                    
                    all_usernames = []
                    for session in sessions:
                        # Directly use the sign_ups dictionary
                        usernames = list(session.sign_ups.values())
                        all_usernames.extend(usernames)

                    username_counts = Counter(all_usernames)
                    top_five_drafters = username_counts.most_common(5)


                    drafter_counts = Counter()
                    for session in sessions:
                        undefeated_drafters = list(session.trophy_drafters) if session.trophy_drafters else []
                        drafter_counts.update(undefeated_drafters)

                    # Format the drafter names and their counts for display
                    undefeated_drafters_field_value = "\n".join([f"{drafter} x{count}" if count > 1 else drafter for drafter, count in drafter_counts.items()])


                    total_drafts = len(sessions)

                    date_str = start_time.strftime("%B %d, %Y")
                    top_drafters_field_value = "\n".join([f"**{name}:** {count} drafts" for name, count in top_five_drafters])
                    embed = discord.Embed(title=f"Open Queue Daily Results - {date_str}", description="", color=discord.Color.dark_purple())
                    embed.add_field(name="**Completed Drafts**", value=total_drafts, inline=False)
                    embed.add_field(name="**Top 5 Drafters**\n", value=top_drafters_field_value, inline=False)
                    embed.add_field(name="**Trophy Drafters**", value=undefeated_drafters_field_value or "No trophies :(", inline=False)

                    await channel.send(embed=embed)



                
async def post_standings(interaction):
    time = datetime.now()
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Fetch teams ordered by PointsEarned (DESC), MatchesCompleted (ASC), and PreseasonPoints (DESC)
            stmt = (select(Team)
                .where(Team.MatchesCompleted >= 1)
                .order_by(Team.PointsEarned.desc(), Team.MatchesCompleted.asc(), Team.PreseasonPoints.desc()))
            results = await session.execute(stmt)
            teams = results.scalars().all()
            
            # Check if teams exist
            if not teams:
                await interaction.response.send_message("No teams have been registered yet.", ephemeral=True)
                return
            
            embed = discord.Embed(title="Team Standings", description=f"Standings as of <t:{int(time.timestamp())}:F>", color=discord.Color.gold())
            last_points = None
            last_matches = None
            last_preseason = None
            actual_rank = 0
            display_rank = 0
            
            # Iterate through teams to build the ranking
            for team in teams:
                # Increase actual_rank each loop, this is the absolute position in the list
                actual_rank += 1
                # Only increase display_rank if the current team's stats do not match the last team's stats
                if (team.PointsEarned, team.MatchesCompleted, team.PreseasonPoints) != (last_points, last_matches, last_preseason):
                    display_rank = actual_rank
                last_points = team.PointsEarned
                last_matches = team.MatchesCompleted
                last_preseason = team.PreseasonPoints

                # Check if the rank should be displayed as tied
                rank_text = f"T{display_rank}" if actual_rank != display_rank else str(display_rank)
                
                preseason_text = f", Preseason Points: {team.PreseasonPoints}" if team.PreseasonPoints > 0 else ""
                embed.add_field(
                    name=f"{rank_text}. {team.TeamName}", 
                    value=f"Points Earned: {team.PointsEarned}, Matches Completed: {team.MatchesCompleted}{preseason_text}", 
                    inline=False
                )
                
                # Limit to top 50 teams in two batches
                if actual_rank == 25:
                    await interaction.response.send_message(embed=embed)
                    embed = discord.Embed(title="Team Standings, Continued", description="", color=discord.Color.gold())
                elif actual_rank == 50:
                    break

            # Send the last batch if it exists
            if actual_rank > 25:
                await interaction.followup.send(embed=embed)


async def weekly_summary(bot):
    pacific_tz = pytz.timezone('US/Pacific')
    now = datetime.now(pacific_tz) - timedelta(days=1)
    start_of_week = pacific_tz.localize(datetime(now.year, now.month, now.day, 0, 0)) - timedelta(days=now.weekday())
    end_of_week = start_of_week + timedelta(days=7)
    print(start_of_week, end_of_week)
    # Define the start date of the league
    start_date = pacific_tz.localize(datetime(2024, 4, 8))
    # Calculate the week number
    week_number = ((start_of_week - start_date).days // 7) + 1

    async with AsyncSessionLocal() as db_session:
        async with db_session.begin():
            # Calculate total number of matches
            match_stmt = select(Match).where(
                Match.MatchDate.between(start_of_week, end_of_week),
                Match.DraftWinnerID.isnot(None)
            )
            match_results = await db_session.execute(match_stmt)
            total_matches = len(match_results.scalars().all())

            # Fetch unique players
            session_stmt = select(DraftSession).where(
                DraftSession.teams_start_time.between(start_of_week, end_of_week),
                DraftSession.premade_match_id.isnot(None)
            )
            session_results = await db_session.execute(session_stmt)
            unique_players = set()
            for session in session_results.scalars():
                unique_players.update(session.sign_ups.keys())
            total_unique_players = len(unique_players)

            # Fetch top 10 standings
            team_stmt = (select(WeeklyLimit)
                         .where(WeeklyLimit.WeekStartDate == start_of_week)
                         .order_by(WeeklyLimit.PointsEarned.desc(), WeeklyLimit.MatchesPlayed.asc())
                         .limit(10))
            team_results = await db_session.execute(team_stmt)
            teams = team_results.scalars().all()

            embed = discord.Embed(title=f"Week {week_number} Summary", description="Divination Team Draft League", color=discord.Color.blue())
            embed.add_field(name="Total Matches", value=str(total_matches), inline=False)
            embed.add_field(name="Unique Players", value=str(total_unique_players), inline=False)

            if teams:
                standings_text = ""
                for index, team in enumerate(teams, 1):
                    standings_text += f"{index}. {team.TeamName} - Points: {team.PointsEarned}, Matches: {team.MatchesPlayed}\n"
                embed.add_field(name="Top 10 Weekly Peformers", value=standings_text, inline=False)
            else:
                embed.add_field(name="Top 10 Weekly Peformers", value="No matches registered.", inline=False)

            # Send the embed to the appropriate channel
            for guild in bot.guilds:
                channel = discord.utils.get(guild.text_channels, name="league-summary")
                if channel:
                    await channel.send(embed=embed)

class ConfigCategorySelector(discord.ui.View):
    def __init__(self, config):
        super().__init__(timeout=300)  # 5 minute timeout
        self.config = config
        
        # Create the category dropdown
        self.add_item(CategoryDropdown(config))


class CategoryDropdown(discord.ui.Select):
    def __init__(self, config):
        # Create options for each top-level config category
        options = []
        for category in config.keys():
            if isinstance(config[category], dict):
                # Only include dictionary items (categories of settings)
                label = category.replace("_", " ").title()  # Format as readable text
                options.append(discord.SelectOption(
                    label=label,
                    value=category,
                    description=f"Configure {label} settings"
                ))
        
        super().__init__(
            placeholder="Select a configuration category",
            min_values=1,
            max_values=1,
            options=options
        )
    
    async def callback(self, interaction: discord.Interaction):
        # Get the selected category
        category = self.values[0]
        
        # Create a new view to show settings in this category
        from config import get_config
        config = get_config(interaction.guild_id)
        view = SettingSelector(config, category)
        
        await interaction.response.edit_message(
            content=f"Select a {category.replace('_', ' ').title()} setting to modify:",
            view=view
        )


class SettingSelector(discord.ui.View):
    def __init__(self, config, category):
        super().__init__(timeout=300)
        self.config = config
        self.category = category
        
        # Add a dropdown for specific settings
        self.add_item(SettingDropdown(config, category))
        
        # Add a back button
        self.add_item(BackButton())


class SettingDropdown(discord.ui.Select):
    def __init__(self, config, category):
        self.category = category
        
        # Create options for each setting in this category
        options = []
        for setting, value in config[category].items():
            # Only include simple values as configurable settings
            if isinstance(value, (str, int, bool, float)) or value is None:
                label = setting.replace("_", " ").title()
                current_value = str(value)
                # Truncate long values
                if len(current_value) > 50:
                    current_value = current_value[:47] + "..."
                
                options.append(discord.SelectOption(
                    label=label,
                    value=setting,
                    description=f"Current: {current_value}"
                ))
        
        super().__init__(
            placeholder=f"Select a {category} setting",
            min_values=1,
            max_values=1,
            options=options
        )
    
    async def callback(self, interaction: discord.Interaction):
        # Get the selected setting
        setting = self.values[0]
        path = f"{self.category}.{setting}"
        
        # Create a modal to get the new value
        modal = ConfigModal(path)
        await interaction.response.send_modal(modal)


class BackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Back to Categories",
            row=1
        )
    
    async def callback(self, interaction: discord.Interaction):
        # Go back to category selection
        from config import get_config
        config = get_config(interaction.guild_id)
        view = ConfigCategorySelector(config)
        
        await interaction.response.edit_message(
            content="Select a configuration category to modify:",
            view=view
        )


class ConfigModal(discord.ui.Modal):
    def __init__(self, setting_path):
        self.setting_path = setting_path
        setting_name = setting_path.split('.')[-1].replace('_', ' ').title()
        
        super().__init__(title=f"Configure {setting_name}")
        
        self.value_input = discord.ui.InputText(
            label=f"New value for {setting_name}",
            placeholder="Enter the new value",
            required=True
        )
        self.add_item(self.value_input)
    
    async def callback(self, interaction: discord.Interaction):
        # Get the new value
        new_value = self.value_input.value
        
        try:
            # Try to convert to appropriate type
            # For booleans
            if new_value.lower() in ["true", "yes", "1", "on"]:
                new_value = True
            elif new_value.lower() in ["false", "no", "0", "off"]:
                new_value = False
            # For numbers
            elif new_value.isdigit():
                new_value = int(new_value)
            elif new_value.replace(".", "", 1).isdigit() and new_value.count(".") == 1:
                new_value = float(new_value)
            
            # Update the config
            from config import update_setting
            success = update_setting(interaction.guild_id, self.setting_path, new_value)
            
            if success:
                await interaction.response.send_message(
                    f"✅ Setting `{self.setting_path}` updated to `{new_value}`",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"❌ Failed to update setting `{self.setting_path}`. This setting may be restricted.",
                    ephemeral=True
                )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Error updating config: {e}",
                ephemeral=True
            )

class SetupWelcomeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)  # 10 minute timeout
    
    @discord.ui.button(label="Start Setup", style=discord.ButtonStyle.primary)
    async def start_button(self, button, interaction):
        # Begin the actual setup process
        await interaction.response.edit_message(
            content="## Step 1: Draft Channels Category\n\n"
                    "Would you like to create a category for draft-related channels?",
            view=SetupCategoryView()
        )


class SetupCategoryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)
        self.config = {
            "categories": {},
            "channels": {},
            "roles": {}
        }
    
    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes_button(self, button, interaction):
        # Save this choice
        self.config["categories"]["draft"] = True
        
        # Create the category name modal
        modal = CategoryNameModal(self.config)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no_button(self, button, interaction):
        # Skip creating category
        self.config["categories"]["draft"] = False
        
        # Move to the next step
        await interaction.response.edit_message(
            content="## Step 2: Draft Channels\n\n"
                    "Which draft-related channels would you like to create?",
            view=SetupChannelsView(self.config)
        )


class CategoryNameModal(discord.ui.Modal):
    def __init__(self, config):
        super().__init__(title="Category Name")
        self.config = config
        
        self.category_name = discord.ui.InputText(
            label="Category Name",
            placeholder="Draft Channels",
            required=True,
            value="Draft Channels"  # Changed from default to value
        )
        self.add_item(self.category_name)
    
    async def callback(self, interaction):
        # Save the category name
        self.config["categories"]["draft_name"] = self.category_name.value
        
        # Move to channels setup
        await interaction.response.edit_message(
            content="## Step 2: Draft Channels\n\n"
                    "Which draft-related channels would you like to create?",
            view=SetupChannelsView(self.config)
        )

class SetupChannelsView(discord.ui.View):
    def __init__(self, config):
        super().__init__(timeout=600)
        self.config = config
        
        # Add channel checkboxes
        self.add_item(ChannelSelect())
    
    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def continue_button(self, button, interaction):
        # Save selected channels
        select = [item for item in self.children if isinstance(item, ChannelSelect)][0]
        self.config["channels"]["selected"] = select.values
        
        # Move to roles setup
        await interaction.response.edit_message(
            content="## Step 3: Server Roles\n\n"
                    "In the next step, you will choose the role that you want to have admin access to draft features",
            view=SetupRolesView(self.config)
        )


class ChannelSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="Team Draft Results",
                value="team_draft_results",
                description="Channel for draft results",
                default=True
            ),
            discord.SelectOption(
                label="Draft Queue",
                value="draft_queue",
                description="Channel for active draft queues",
                default=True
            )
        ]
        
        super().__init__(
            placeholder="Select channels to create",
            min_values=0,
            max_values=len(options),
            options=options
        )
    
    async def callback(self, interaction):
        # This just updates the selection - continue button handles saving
        await interaction.response.defer()


class SetupRolesView(discord.ui.View):
    def __init__(self, config):
        super().__init__(timeout=600)
        self.config = config
    
    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def continue_button(self, button, interaction):
        # Get guild roles for selection
        guild_roles = interaction.guild.roles
        
        # Filter out @everyone and integration roles
        selectable_roles = [role for role in guild_roles 
                        if not role.is_default() and not role.is_integration()]
        
        # Create role options (up to 25 which is the Discord limit)
        options = []
        for role in selectable_roles[:25]:
            options.append(
                discord.SelectOption(
                    label=role.name,
                    value=role.name,
                    description=f"Role ID: {role.id}"
                )
            )
        
        # If no roles found, skip to matchmaking
        if not options:
            await interaction.response.edit_message(
                content="No custom roles found in this server.\n\nMoving to team balancing...",
                view=SetupMatchmakingView(self.config)
            )
            return
        
        # Create a new view with the role select
        role_view = discord.ui.View(timeout=300)
        
        # Create the select
        role_select = discord.ui.Select(
            placeholder="Select an admin role",
            options=options,
            min_values=0,
            max_values=1
        )
        
        # Define callback within this scope to capture needed variables
        async def role_select_callback(role_interaction):
            if role_select.values:
                # Save selected role
                self.config["roles"]["selected"] = role_select.values
            else:
                # No role selected
                self.config["roles"]["selected"] = []
            
            # Now move to matchmaking view
            await role_interaction.response.edit_message(
                content="## Step 4: Team Balancing\n\n"
                        "How often should the bot use skill-based team balancing?\n\n"
                        "0% = Always random teams\n"
                        "100% = Always balanced teams\n"
                        "50% = Mix of both",
                view=SetupMatchmakingView(self.config)
            )
        
        # Assign callback
        role_select.callback = role_select_callback
        
        # Add select to view
        role_view.add_item(role_select)
        
        # Show the role selection - THIS IS THE ONLY RESPONSE TO THE ORIGINAL INTERACTION
        await interaction.response.edit_message(
            content="## Step 3: Admin Role\n\n"
                    "Select a role that should have admin access to draft features:\n"
                    "(Leave unselected for no special admin role)",
            view=role_view
        )

class RoleSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="Cube Overseer",
                value="cube_overseer",
                description="Admin role for draft management",
                default=True
            )
        ]
        
        super().__init__(
            placeholder="Select roles to create",
            min_values=0,
            max_values=len(options),
            options=options
        )
    
    async def callback(self, interaction):
        # This just updates the selection - continue button handles saving
        await interaction.response.defer()

class SetupMatchmakingView(discord.ui.View):
    def __init__(self, config):
        super().__init__(timeout=600)
        self.config = config

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def continue_button(self, button, interaction):
        # Show a modal for inputting TrueSkill percentage
        modal = MatchmakingModal(self.config)
        await interaction.response.send_modal(modal)

class MatchmakingModal(discord.ui.Modal):
    def __init__(self, config):
        super().__init__(title="Team Balancing Settings")
        self.config = config
        
        self.percentage = discord.ui.InputText(
            label="TrueSkill Matchmaking Percentage (0-100)",
            placeholder="0 = always random, 100 = always balanced",
            required=True,
            value="0"
        )
        self.add_item(self.percentage)
    
    async def callback(self, interaction):
        try:
            # Parse the percentage value (handle both "50" and "50%")
            value = self.percentage.value.strip().rstrip('%')
            percentage = float(value)
            
            # Validate the range
            if percentage < 0 or percentage > 100:
                await interaction.response.send_message(
                    "⚠️ Please enter a number between 0 and 100.",
                    ephemeral=True
                )
                return
            
            # Save the setting
            self.config["matchmaking"] = {"trueskill_chance": percentage}
            
            # Show confirmation with updated summary
            await interaction.response.edit_message(
                content="## Setup Summary\n\n"
                        "Here's what will be created based on your choices:",
                view=SetupConfirmView(self.config)
            )
            
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Invalid input. Please enter a number between 0 and 100.",
                ephemeral=True
            )

class SetupConfirmView(discord.ui.View):
    def __init__(self, config):
        super().__init__(timeout=600)
        self.config = config
        
        # Create summary text
        summary = "# Configuration Summary\n\n"
        
        # Category summary
        if config["categories"].get("draft", False):
            category_name = config["categories"].get("draft_name", "Draft Channels")
            summary += f"📁 Category: **{category_name}**\n\n"
        else:
            summary += "📁 No new category will be created\n\n"
        
        # Channels summary
        selected_channels = config["channels"].get("selected", [])
        if selected_channels:
            summary += "💬 Channels to create:\n"
            channel_labels = {
                "team_draft_results": "team-draft-results",
                "draft_queue": "draft-queue"
            }
            for channel in selected_channels:
                summary += f"- {channel_labels.get(channel, channel)}\n"
            summary += "\n"
        else:
            summary += "💬 No new channels will be created\n\n"
        
        # Roles summary
        selected_roles = config["roles"].get("selected", [])
        if selected_roles:
            summary += "🏷️ Admin role to use:\n"
            for role in selected_roles:
                summary += f"- {role}\n"
            summary += "\n"
        else:
            summary += "🏷️ No admin role selected\n\n"
        
        # Matchmaking summary
        matchmaking_chance = config.get("matchmaking", {}).get("trueskill_chance", 0)
        summary += f"⚖️ Team Balancing: **{matchmaking_chance}%** skill-based\n\n"
        # Set the summary as the message content
        self.summary = summary
    
    @discord.ui.button(label="Confirm & Create", style=discord.ButtonStyle.success)
    async def confirm_button(self, button, interaction):
        # Create everything
        created_items = []
        guild = interaction.guild
        
        # Create category if needed
        category = None
        if self.config["categories"].get("draft", False):
            category_name = self.config["categories"].get("draft_name", "Draft Channels")
            category = await guild.create_category(category_name)
            created_items.append(f"📁 Category: {category.name}")
        
        # Create channels - ensure selected_channels is a list
        selected_channels = self.config.get("channels", {}).get("selected", []) or []
        
        for channel_key in selected_channels:
            channel_names = {
                "team_draft_results": "team-draft-results",
                "draft_queue": "draft-queue"
            }
            channel_name = channel_names.get(channel_key, channel_key)
            
            # Create in the category if it exists
            if category:
                channel = await guild.create_text_channel(channel_name, category=category)
            else:
                channel = await guild.create_text_channel(channel_name)
                
            created_items.append(f"💬 Channel: {channel.name}")
        
        # Save the configuration
        from config import get_config, save_config
        guild_config = get_config(guild.id)
        
        # Update with new settings
        if category:
            guild_config["categories"]["draft"] = category_name
        
        for channel_key in selected_channels:
            channel_names = {
                "team_draft_results": "team-draft-results",
                "draft_queue": "draft-queue"
            }
            guild_config["channels"][channel_key] = channel_names.get(channel_key, channel_key)
        
        # Ensure selected_roles is a list
        selected_roles = self.config.get("roles", {}).get("selected", []) or []
        
        if selected_roles:
            guild_config["roles"]["admin"] = selected_roles[0]
        
        # Save updated config
        save_config(guild.id, guild_config)
        
        # Show success message
        created_items_text = "\n".join(created_items) if created_items else "No items needed to be created."
        await interaction.response.edit_message(
            content=f"# Setup Complete! 🎉\n\n"
                f"The following items have been created:\n\n"
                f"{created_items_text}\n\n"
                f"Your draft bot is now ready to use!",
            view=None
        )
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, button, interaction):
        await interaction.response.edit_message(
            content="Setup wizard cancelled. No changes were made.",
            view=None
        )