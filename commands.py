import discord
from session import register_team_to_db, Team, AsyncSessionLocal, Match, MatchResult, DraftSession
from sqlalchemy import select, not_
import aiocron
import pytz
from datetime import datetime, timedelta
from collections import Counter

async def league_commands(bot):

    @bot.slash_command(name="registerteam", description="Register a new team in the league")
    async def register_team(interaction: discord.Interaction, team_name: str):
        team, response_message = await register_team_to_db(team_name)
        await interaction.response.send_message(response_message, ephemeral=True)
    
    @bot.slash_command(name='listteams', description='List all registered teams')
    async def list_teams(interaction: discord.Interaction):
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Fetch all teams sorted by their name
                stmt = select(Team).order_by(Team.TeamName.asc())
                result = await session.execute(stmt)
                teams = result.scalars().all()

            # If there are no teams registered
            if not teams:
                await interaction.response.send_message("No teams have been registered yet.", ephemeral=True)
                return

            # Create an embed to list all teams
            embed = discord.Embed(title="Registered Teams", description="", color=discord.Color.blue())
            
            # Adding each team to the embed description
            for team in teams:
                embed.description += f"- {team.TeamName}\n"

            await interaction.response.send_message(embed=embed)

    @bot.slash_command(name='list_commands', description='Lists all available slash commands')
    async def list_commands(ctx):
        # Manually creating a list of commands and descriptions
        commands_list = {
            "`/list_commands`": "Lists all available slash commands.\n",
            "**Lobby Commands**" : "",
            "**`/startdraft`**": "Launch a lobby for randomized team drafts.",
            "**`/leaguedraft`**": "Launch a lobby for League Drafts (results tracked)",
            "**`/premadedraft`**": "Launch a lobby for premade teams (untracked)\n",
            "**League Commands**": "",
            "**`/post_challenge`**": "Set a draft time for other teams to challenge your team.",
            "**`/registerteam`**": "Register your team for the league",
            "**`/listteams`**": "Displays registered teams",
            "**`/standings`**": "Displays current league standings"
        }
        
        # Formatting the list for display
        commands_description = "\n".join([f"{cmd}: {desc}" for cmd, desc in commands_list.items()])
        
        # Creating an embed to nicely format the list of commands
        embed = discord.Embed(title="Available Commands", description=commands_description, color=discord.Color.blue())
        
        await ctx.respond(embed=embed)
    

    @bot.slash_command(name="post_challenge", description="Post a challenge for your team")
    async def postchallenge(interaction: discord.Interaction):

            from league import InitialPostView
            initial_view = InitialPostView()
            await interaction.response.send_message("Please select the range for your team", view=initial_view, ephemeral=True)

    @bot.slash_command(name='standings', description='Display the team standings by points earned')
    async def standings(interaction: discord.Interaction):
        await post_standings(interaction)



    @bot.slash_command(name="leaguedraft", description="Start a league draft with chosen teams and cube.")
    async def leaguedraft(interaction: discord.Interaction):
        from league import InitialRangeView   
        initial_view = InitialRangeView()
        await interaction.response.send_message("Step 1 of 2: Please select the range for your team and the opposing team:", view=initial_view, ephemeral=True)
        

    @aiocron.crontab('01 09 * * *', tz=pytz.timezone('US/Eastern'))
    async def daily_league_results():
        # Fetch all guilds the bot is in and look for the "league-summary" channel
        for guild in bot.guilds:
            channel = discord.utils.get(guild.text_channels, name="league-summary")
            if channel:
                break  # If we find the channel, we exit the loop
        
        if not channel:  # If the bot cannot find the channel in any guild, log an error and return
            print("Error: 'league-summary' channel not found.")
            return

        eastern_tz = pytz.timezone('US/Eastern')
        now = datetime.now(eastern_tz)
        start_time = eastern_tz.localize(datetime(now.year, now.month, now.day, 1, 0)) - timedelta(days=1)  # 3 AM previous day
        end_time = start_time + timedelta(hours=24)  # 3 AM current day

        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                stmt = select(Match).where(Match.MatchDate.between(start_time, end_time),
                                        not_(Match.DraftWinnerID == None))
                results = await db_session.execute(stmt)
                matches = results.scalars().all()

                if not matches:
                    await channel.send("No matches found in the last 24 hours.")
                    return
                
                trophy_drafter_stmt = select(DraftSession).where(DraftSession.teams_start_time.between(start_time, end_time),
                                                                 not_(DraftSession.premade_match_id),
                                                                 DraftSession.tracked_draft==1)
                trophy_results = await db_session.execute(trophy_drafter_stmt)
                trophy_sessions = trophy_results.scalars().all()

                drafter_counts = Counter()
                for session in trophy_sessions:
                    undefeated_drafters = list(session.trophy_drafters) if session.trophy_drafters else []
                    drafter_counts.update(undefeated_drafters)

                undefeated_drafters_field_value = "\n".join([f"{drafter} x{count}" if count > 1 else drafter for drafter, count in drafter_counts.items()])


                date_str = start_time.strftime("%B %d, %Y")
                embed = discord.Embed(title=f"Daily League Results - {date_str}", description="", color=discord.Color.blue())
                for match in matches:
                    result_line = f"**{match.TeamAName}** defeated **{match.TeamBName}** ({match.TeamAWins} - {match.TeamBWins})" if match.TeamAWins > match.TeamBWins else f"**{match.TeamBName}** defeated **{match.TeamAName}** ({match.TeamBWins} - {match.TeamAWins})"
                    embed.description += result_line + "\n"
                embed.add_field(name="**Trophy Drafters**", value=undefeated_drafters_field_value or "None", inline=False)
                await channel.send(embed=embed)

    @aiocron.crontab('15 09 * * *', tz=pytz.timezone('US/Eastern'))
    async def daily_random_results():
        # Fetch all guilds the bot is in and look for the "league-summary" channel
        for guild in bot.guilds:
            channel = discord.utils.get(guild.text_channels, name="daily-summary-open-queue")
            if channel:
                break  # If we find the channel, we exit the loop
        
        if not channel:  # If the bot cannot find the channel in any guild, log an error and return
            print("Error: 'daily-summary-open-queue' channel not found.")
            return

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
                    DraftSession.session_type == "random"
                )
                result = await db_session.execute(stmt)
                sessions = result.scalars().all()

                if not sessions:
                    await channel.send("No matches found in the last 24 hours.")
                    return
                
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
                embed = discord.Embed(title=f"Open Queue Daily Results - {date_str}", description="", color=discord.Color.blue())
                embed.add_field(name="**Completed Drafts**", value=total_drafts, inline=False)
                embed.add_field(name="**Top 5 Drafters**\n", value=top_drafters_field_value, inline=False)
                embed.add_field(name="**Trophy Drafters**", value=undefeated_drafters_field_value or "No trophies :(", inline=False)

                await channel.send(embed=embed)
        
    @aiocron.crontab('00 09 * * *', tz=pytz.timezone('US/Eastern'))
    async def post_league_standings():
        # Fetch all guilds the bot is in and look for the "league-summary" channel
        for guild in bot.guilds:
            channel = discord.utils.get(guild.text_channels, name="league-summary")
            if channel:
                break  # If we find the channel, we exit the loop
        
        if not channel:  # If the bot cannot find the channel in any guild, log an error and return
            print("Error: 'league-summary' channel not found.")
            return
        
        time = datetime.now()
        count = 1
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Fetch teams ordered by PointsEarned (DESC) and MatchesCompleted (ASC)
                stmt = (select(Team)
                    .where(Team.MatchesCompleted >= 1)
                    .order_by(Team.PointsEarned.desc(), Team.MatchesCompleted.asc()))
                results = await session.execute(stmt)
                teams = results.scalars().all()
                
                # Check if teams exist
                if not teams:
                    await channel.send("No results posted yet.")
                    return

                # Format the standings as an embed
                embed = discord.Embed(title="Team Standings", description=f"Standings as of <t:{int(time.timestamp())}:F>", color=discord.Color.gold())
                for team in teams:
                    embed.add_field(name=f"{count}. {team.TeamName}", value=f"Points Earned: {team.PointsEarned}, Matches Completed: {team.MatchesCompleted}", inline=False)
                    count += 1
                await channel.send(embed=embed)

                
async def post_standings(interaction):
    time = datetime.now()
    count = 1
    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Fetch teams ordered by PointsEarned (DESC) and MatchesCompleted (ASC)
            stmt = (select(Team)
                .where(Team.MatchesCompleted >= 1)
                .order_by(Team.PointsEarned.desc(), Team.MatchesCompleted.asc()))
            results = await session.execute(stmt)
            teams = results.scalars().all()
            
            # Check if teams exist
            if not teams:
                await interaction.response.send_message("No teams have been registered yet.", ephemeral=True)
                return

            # Format the standings as an embed
            embed = discord.Embed(title="Team Standings", description=f"Standings as of <t:{int(time.timestamp())}:F>", color=discord.Color.gold())
            for team in teams:
                embed.add_field(name=f"{count}. {team.TeamName}", value=f"Points Earned: {team.PointsEarned}, Matches Completed: {team.MatchesCompleted}", inline=False)
                count += 1
            await interaction.response.send_message(embed=embed)