import discord
from session import register_team_to_db, Team, AsyncSessionLocal, Match, DraftSession, remove_team_from_db, TeamRegistration
from sqlalchemy import select, not_
from sqlalchemy.orm.attributes import flag_modified
import aiocron
import pytz
from datetime import datetime, timedelta
from collections import Counter

async def league_commands(bot):

    @bot.slash_command(name="registerteam", description="Register a new team in the league")
    async def register_team(interaction: discord.Interaction, team_name: str):
        team, response_message = await register_team_to_db(team_name)
        await interaction.response.send_message(response_message, ephemeral=True)
    
    @bot.slash_command(name="delete_team", description="Mod Only: Remove a new team from the league")
    async def deleteteam(ctx, *, team_name: str):
        await ctx.defer()  # Acknowledge the interaction immediately to prevent timeout
        response_message = await remove_team_from_db(ctx, team_name)
        await ctx.followup.send(response_message)

    @bot.slash_command(name='list_teams', description='List all registered teams')
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

    @bot.slash_command(name='commands', description='Lists all available slash commands')
    async def list_commands(ctx):
        # Manually creating a list of commands and descriptions
        commands_list = {
            "`/commands`": "Lists all available slash commands.\n",
            "**Lobby Commands**" : "",
            "**`/startdraft`**": "Launch a lobby for randomized team drafts.",
            "**`/leaguedraft`**": "Launch a lobby for League Drafts (results tracked)",
            "**`/premadedraft`**": "Launch a lobby for premade teams (untracked)\n",
            "**League Commands**": "",
            "**`/post_challenge`**": "Set a draft time for other teams to challenge your team.",
            "**`/list_challenges`**": "Lists all open challenges with a link to sign up.",
            "**`/list_teams`**": "Displays registered teams",
            "**`/find_a_match`**": "Choose a time to find challenges within 2 hours of chosen time.",
            "**`/standings`**": "Displays current league standings\n",
            "**Open Queue Commands**": "",
            "**`/trophies`**": "Displays this month's trophy leaderboard",
            "**Mod Commands**": "",
            "**`/delete_team`**": "Removes a registered team",
            "**`/registerteam`**": "Register your team for the league",
            "**`/register_player`**": "Register a player to a team",
            "**`/remove_player`**": "Removes a player from all teams",
        }
        
        # Formatting the list for display
        commands_description = "\n".join([f"{cmd}: {desc}" for cmd, desc in commands_list.items()])
        
        # Creating an embed to nicely format the list of commands
        embed = discord.Embed(title="Available Commands", description=commands_description, color=discord.Color.blue())
        
        await ctx.respond(embed=embed)
    

    @bot.slash_command(name="post_challenge", description="Post a challenge for your team")
    async def postchallenge(interaction: discord.Interaction):
        user_id_str = str(interaction.user.id)
        async with AsyncSessionLocal() as session:  # Assuming AsyncSessionLocal is your session maker
            async with session.begin():
                # Query for any team registration entries that include the user ID in their TeamMembers
                stmt = select(TeamRegistration).where(TeamRegistration.TeamMembers.contains(user_id_str))
                result = await session.execute(stmt)
                team_registration = result.scalars().first()

                if team_registration:
                    # Extracting user details
                    team_id = team_registration.TeamID
                    team_name = team_registration.TeamName
                    user_display_name = team_registration.TeamMembers.get(user_id_str)
                    from league import InitialPostView
                    initial_view = InitialPostView(command_type="post", team_id=team_id, team_name=team_name, user_display_name=user_display_name)
                    await interaction.response.send_message(f"Post a Challenge for {team_name}. Select Cube and Timezone.", view=initial_view, ephemeral=True)
                else:
                    await interaction.response.send_message(f"You are not registered to a team. Contact a Cube Overseer if this is an error.", ephemeral=True)


    @bot.slash_command(
    name="remove_user_from_team",
    description="Remove a user from all teams they are assigned to"
    )
    @discord.option(
        "user_id",
        description="The Discord user ID of the member to remove from teams",
        required=True
    )
    async def remove_user_from_team(interaction: discord.Interaction, user_id: str):
        # Check if the user has the "Cube Overseer" role
        cube_overseer_role_name = "Cube Overseer"
        if cube_overseer_role_name not in [role.name for role in interaction.user.roles]:
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Convert user_id to str if not already to ensure consistency in comparison
                user_id_str = str(user_id)
                # Query for any team registration entries that include the user ID in their TeamMembers
                stmt = select(TeamRegistration)
                all_team_registrations = await session.execute(stmt)
                teams_updated = 0

                for team_registration in all_team_registrations.scalars().all():
                    if user_id_str in team_registration.TeamMembers:
                        # Remove the user from the TeamMembers dictionary
                        print(team_registration.TeamMembers[user_id_str])
                        del team_registration.TeamMembers[user_id_str]
                        flag_modified(team_registration, "TeamMembers")
                        session.add(team_registration)
                        teams_updated += 1

                await session.commit()

        if teams_updated > 0:
            await interaction.response.send_message(f"User {user_id} was successfully removed from {teams_updated} teams.", ephemeral=True)
        else:
            await interaction.response.send_message(f"User {user_id} was not found in any teams.", ephemeral=True)


    @bot.slash_command(name="register_player", description="Post a challenge for your team")
    async def registerplayer(interaction: discord.Interaction):
        cube_overseer_role = discord.utils.get(interaction.guild.roles, name="Cube Overseer")
    
        if cube_overseer_role in interaction.user.roles:
            from league import InitialPostView
            initial_view = InitialPostView(command_type="register")
            await interaction.response.send_message("Please select the range for the team", view=initial_view, ephemeral=True)
        else:
            # Responding with a message indicating lack of permission
            await interaction.response.send_message("You do not have permission to register players, please tag Cube Overseer if you need to make changes.", ephemeral=True)

            
    @bot.slash_command(name="find_a_match", description="Find an open challenge based on a given time.")
    async def findamatch(interaction: discord.Interaction):

            from league import InitialPostView
            initial_view = InitialPostView(command_type="find")
            await interaction.response.send_message("Please select the range for your team", view=initial_view, ephemeral=True)


    @bot.slash_command(name="list_challenges", description="List all open challenges in chronological order.")
    async def list_challenge(interaction: discord.Interaction):
        async with AsyncSessionLocal() as db_session: 
            async with db_session.begin():
                from session import Challenge
                range_stmt = select(Challenge).where(Challenge.team_b == None,
                                                    Challenge.message_id != None
                                                    ).order_by(Challenge.start_time.asc())
                                                
                results = await db_session.execute(range_stmt)
                challenges = results.scalars().all()

                if not challenges:
                # No challenges found within the range
                    await interaction.followup.send("No open challenges. Consider using /post_challenge to open a challenge yourself!", ephemeral=True)
                    return
                # Construct the link to the original challenge message
                
                embed = discord.Embed(title="Open Challenges", description="Here are all open challenges", color=discord.Color.blue())

                for challenge in challenges:
                    message_link = f"https://discord.com/channels/{challenge.guild_id}/{challenge.channel_id}/{challenge.message_id}"
                    # Mention the initial user who posted the challenge
                    initial_user_mention = f"<@{challenge.initial_user}>"
                    # Format the start time of each challenge to display in the embed
                    time = datetime.strptime(str(challenge.start_time), "%Y-%m-%d %H:%M:%S")
                    utc_zone = pytz.timezone("UTC")
                    start_time = utc_zone.localize(time)
                    formatted_time = f"<t:{int(start_time.timestamp())}:F>"
                    relative_time = f"<t:{int(start_time.timestamp())}:R>"
                    embed.add_field(name=f"Team: {challenge.team_a}", value=f"Time: {formatted_time} ({relative_time})\nCube: {challenge.cube}\nPosted by: {initial_user_mention}\n[Sign Up Here!]({message_link})", inline=False)
                await interaction.response.send_message(embed=embed)


    @bot.event
    async def on_reaction_add(reaction, user):
        # Check if the reaction is in the role-request channel
        if reaction.message.channel.name == 'role-request':
            # Ensure user is a Member object
            if reaction.message.guild:
                member = await reaction.message.guild.fetch_member(user.id)
                # Check if the user has no roles other than @everyone
                if len(member.roles) == 1:
                    # Find the 'suspected bot' role in the guild
                    suspected_bot_role = discord.utils.get(member.guild.roles, name='suspected bot')
                    if suspected_bot_role:
                        try:
                            await member.add_roles(suspected_bot_role)
                            print(f"Assigned 'suspected bot' role to {member.name}")
                        except discord.Forbidden:
                            print(f"Permission error: Unable to assign roles. Check the bot's role position and permissions.")
                        except discord.HTTPException as e:
                            print(f"HTTP exception occurred: {e}")



    @bot.slash_command(name='standings', description='Display the team standings by points earned')
    async def standings(interaction: discord.Interaction):
        await post_standings(interaction)

    @bot.slash_command(name="trophies", description="Display the Trophy Leaderboard for the current month.")
    async def trophies(ctx):
        eastern_tz = pytz.timezone('US/Eastern')
        now = datetime.now(eastern_tz)
        # Get the first day of the current month at 00:00 hours
        first_day_of_month = eastern_tz.localize(datetime(now.year, now.month, 1))

        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                stmt = select(DraftSession).where(
                    DraftSession.teams_start_time.between(first_day_of_month, now),
                    not_(DraftSession.trophy_drafters == None),
                    DraftSession.session_type == "random"
                )
                results = await db_session.execute(stmt)
                trophy_sessions = results.scalars().all()

                drafter_counts = Counter()
                for session in trophy_sessions:
                    undefeated_drafters = session.trophy_drafters if session.trophy_drafters else []
                    drafter_counts.update(undefeated_drafters)

                sorted_drafters = drafter_counts.most_common()

                embed = discord.Embed(title=f"{now.strftime('%B')} Trophy Leaderboard",
                                    description="Earn Trophies in Open-Queue",
                                    color=discord.Color.blue())

                last_count = None
                rank = 0
                skip_next_rank = 0

                for drafter, count in sorted_drafters:
                    if count == last_count:  # If this user has the same trophy count as the last one
                        skip_next_rank += 1  # Increase skip for the next unique trophy count
                        display_rank = f"T{rank}"  # Display rank with a "T" for tie
                    else:
                        rank += 1 + skip_next_rank  # Increment rank, accounting for any skipped ranks
                        skip_next_rank = 0  # Reset skip rank counter
                        display_rank = str(rank)  # Display rank as usual
                        last_count = count  # Update the last_count to the current user's trophy count

                    rank_title = f"{display_rank}. {drafter}"
                    embed.add_field(name=rank_title, value=f"Trophies: {count}", inline=False)
                
                await ctx.respond(embed=embed)

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

    @aiocron.crontab('00 13 * * *', tz=pytz.timezone('US/Eastern'))  
    async def post_todays_matches():
        for guild in bot.guilds:
            channel = discord.utils.get(guild.text_channels, name="league-summary")
            if channel:
                break  # If we find the channel, we exit the loop
        
        if not channel:  # If the bot cannot find the channel in any guild, log an error and return
            print("Error: 'league-summary' channel not found.")
            return
        
        eastern = pytz.timezone('US/Eastern')
        now = datetime.now(eastern).replace(hour=13, minute=0, second=0, microsecond=0)
        tomorrow = now + timedelta(days=1)

        # Convert times to UTC as your database stores times in UTC
        now_utc = now.astimezone(pytz.utc)
        tomorrow_utc = tomorrow.astimezone(pytz.utc)

        async with AsyncSessionLocal() as session:
            async with session.begin():
                # Scheduled Matches
                from session import Challenge
                scheduled_stmt = select(Challenge).where(
                    Challenge.start_time.between(now_utc, tomorrow_utc),
                    Challenge.team_b_id.isnot(None)
                ).order_by(Challenge.start_time.asc())
                scheduled_result = await session.execute(scheduled_stmt)
                scheduled_matches = scheduled_result.scalars().all()

                # Open Challenges
                open_stmt = select(Challenge).where(
                    Challenge.start_time.between(now_utc, tomorrow_utc),
                    Challenge.team_b_id.is_(None)
                ).order_by(Challenge.start_time.asc())
                open_result = await session.execute(open_stmt)
                open_challenges = open_result.scalars().all()

                embed = discord.Embed(title="Today's Matches", color=discord.Color.blue())
                # Add fields or descriptions to embed based on scheduled_matches and open_challenges
                embed.add_field(name="Scheduled Matches", value="No Matches Scheduled" if not scheduled_matches else "", inline=False)
                if scheduled_matches:
                    sch_count = 1
                    for match in scheduled_matches:
                        #print(match.guild_id)
                        message_link = f"https://discord.com/channels/{match.guild_id}/{match.channel_id}/{match.message_id}"
                        # Mention the initial user who posted the challenge
                        initial_user_mention = f"<@{match.initial_user}>"
                        opponent_user_mention = f"<@{match.opponent_user}>"
                        # Format the start time of each challenge to display in the embed
                        time = datetime.strptime(str(match.start_time), "%Y-%m-%d %H:%M:%S")
                        utc_zone = pytz.timezone("UTC")
                        start_time = utc_zone.localize(time)
                        formatted_time = f"<t:{int(start_time.timestamp())}:F>"
                        relative_time = f"<t:{int(start_time.timestamp())}:R>"
                        embed.add_field(name=f"{sch_count}. {match.team_a} v. {match.team_b}", value=f"Draft Start Time: {formatted_time} ({relative_time})\nCube: {match.cube}\nTeam Leads: {initial_user_mention} {opponent_user_mention}\n[Challenge Link]({message_link})", inline=False)
                        sch_count += 1

                embed.add_field(name="\n\nOpen Challenges", value="No Open Challenges" if not open_challenges else "", inline=False)
                if open_challenges:
                    open_count = 1
                    for match in open_challenges:
                        #print(match.guild_id)
                        message_link = f"https://discord.com/channels/{match.guild_id}/{match.channel_id}/{match.message_id}"
                        # Mention the initial user who posted the challenge
                        initial_user_mention = f"<@{match.initial_user}>"
                        # Format the start time of each challenge to display in the embed
                        time = datetime.strptime(str(match.start_time), "%Y-%m-%d %H:%M:%S")
                        utc_zone = pytz.timezone("UTC")
                        start_time = utc_zone.localize(time)
                        formatted_time = f"<t:{int(start_time.timestamp())}:F>"
                        relative_time = f"<t:{int(start_time.timestamp())}:R>"
                        embed.add_field(name=f"{open_count}. Team: {match.team_a}", value=f"Proposed Start Time: {formatted_time} ({relative_time})\nCube: {match.cube}\nPosted by: {initial_user_mention}\n[Sign Up Here!]({message_link})", inline=False)
                        open_count += 1
                await channel.send(embed=embed)

    @aiocron.crontab('15 09 * * *', tz=pytz.timezone('US/Eastern'))
    async def daily_random_results():
        # Fetch all guilds the bot is in and look for the "league-summary" channel
        for guild in bot.guilds:
            channel = discord.utils.get(guild.text_channels, name="team-draft-results")
            if channel:
                break  # If we find the channel, we exit the loop
        
        if not channel:  # If the bot cannot find the channel in any guild, log an error and return
            print("Error: 'team-draft-results' channel not found.")
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
                embed = discord.Embed(title=f"Open Queue Daily Results - {date_str}", description="", color=discord.Color.dark_purple())
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
                # Split the results into two sets if necessary
                first_batch = teams[:25]  # Top 25 teams
                second_batch = teams[25:50]  # Next 25 teams if more than 25

                # Send the first batch
                embed = discord.Embed(title="Team Standings", description=f"Top 25 Standings as of <t:{int(time.timestamp())}:F>", color=discord.Color.gold())
                for index, team in enumerate(first_batch, 1):
                    embed.add_field(name=f"{index}. {team.TeamName}", value=f"Points Earned: {team.PointsEarned}, Matches Completed: {team.MatchesCompleted}", inline=False)
                await channel.send(embed=embed)

                # Send the second batch if it exists
                if second_batch:
                    embed2 = discord.Embed(title="Team Standings, Continued", description=f"", color=discord.Color.gold())
                    for index, team in enumerate(second_batch, 26):
                        embed2.add_field(name=f"{index}. {team.TeamName}", value=f"Points Earned: {team.PointsEarned}, Matches Completed: {team.MatchesCompleted}", inline=False)
                    await channel.send(embed=embed2)

                
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
            
            # Split the results into two sets if necessary
            first_batch = teams[:25]  # Top 25 teams
            second_batch = teams[25:50]  # Next 25 teams if more than 25

            # Send the first batch
            embed = discord.Embed(title="Team Standings", description=f"Top 25 Standings as of <t:{int(time.timestamp())}:F>", color=discord.Color.gold())
            for index, team in enumerate(first_batch, 1):
                embed.add_field(name=f"{index}. {team.TeamName}", value=f"Points Earned: {team.PointsEarned}, Matches Completed: {team.MatchesCompleted}", inline=False)
            await interaction.response.send_message(embed=embed)

            # Send the second batch if it exists
            if second_batch:
                embed2 = discord.Embed(title="Team Standings, Continued", description=f"", color=discord.Color.gold())
                for index, team in enumerate(second_batch, 26):
                    embed2.add_field(name=f"{index}. {team.TeamName}", value=f"Points Earned: {team.PointsEarned}, Matches Completed: {team.MatchesCompleted}", inline=False)
                await interaction.followup.send(embed=embed2)