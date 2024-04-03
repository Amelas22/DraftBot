import discord
from session import register_team_to_db, Team, AsyncSessionLocal, Match, MatchResult, DraftSession
from sqlalchemy import select, not_
import aiocron
import pytz
import json
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

    @aiocron.crontab('00 09 * * *', tz=pytz.timezone('US/Eastern'))
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
        start_time = eastern_tz.localize(datetime(now.year, now.month, now.day, 3, 0)) - timedelta(days=1)  # 3 AM previous day
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
                date_str = start_time.strftime("%B %d, %Y")
                embed = discord.Embed(title=f"Daily League Results - {date_str}", description="", color=discord.Color.blue())
                for match in matches:
                    result_line = f"**{match.TeamAName}** defeated **{match.TeamBName}** ({match.TeamAWins} - {match.TeamBWins})" if match.TeamAWins > match.TeamBWins else f"**{match.TeamBName}** defeated **{match.TeamAName}** ({match.TeamBWins} - {match.TeamAWins})"
                    embed.description += result_line + "\n"

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
                    undefeated_drafters = list(session.pairings) if session.pairings else []
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