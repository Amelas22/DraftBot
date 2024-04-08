'''
Code for league draft structure
'''

import discord
import asyncio
import pytz
from discord.ui import Select, View, Modal, InputText
from datetime import datetime, timedelta
from session import AsyncSessionLocal, Team, DraftSession, Match, Challenge, TeamRegistration
from sqlalchemy import select, update
import random


class InitialRangeView(View):
    def __init__(self):
        super().__init__()
        self.your_team_range = None
        self.opponent_team_range = None
        self.add_item(RangeSelect("Your Team Range", "your_team_range"))
        self.add_item(RangeSelect("Opposing Team Range", "opponent_team_range"))

    async def check_and_send_team_cube(self, interaction: discord.Interaction):
        if self.your_team_range and self.opponent_team_range:
            new_view = LeagueDraftView()
            await new_view.your_team_select.populate(self.your_team_range)
            await new_view.opponent_team_select.populate(self.opponent_team_range)
            await interaction.followup.send("Step 2 of 2: Please select the cube and specific teams:", view=new_view, ephemeral=True)

class RangeSelect(Select):
    def __init__(self, placeholder, attribute_name):
        self.attribute_name = attribute_name
        range_choices = [
            discord.SelectOption(label="Team Name: Starts with (A-M)", value="A-M"),
            discord.SelectOption(label="Team Name: Starts with (N-Z)", value="N-Z"),
        ]
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=range_choices)

    async def callback(self, interaction: discord.Interaction):
        try:
            setattr(self.view, self.attribute_name, self.values[0])
            await interaction.response.defer(ephemeral=True)
            await self.view.check_and_send_team_cube(interaction)
        except Exception as e:
            print(f"Error in Team Select callback: {e}")
            

class CubeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="LSVCube", description="Curated by Luis Scott Vargas"),
            discord.SelectOption(label="AlphaFrog", description="Curated by Gavin Thompson"),
            discord.SelectOption(label="mtgovintage", description="Curated by Ryan Spain and Chris Wolf"),
        ]
        super().__init__(placeholder="Choose Cube", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.view.cube_choice = self.values[0]
        await self.view.check_and_send_summary(interaction)        

class TeamSelect(Select):
    def __init__(self, placeholder, attribute_name):
        self.attribute_name = attribute_name
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=[])


    async def callback(self, interaction: discord.Interaction):
        try:
            setattr(self.view, self.attribute_name, self.values[0])
            await interaction.response.defer(ephemeral=True)
            await self.view.check_and_send_summary(interaction)
        except Exception as e:
            print(f"Error in Team Select callback: {e}")


    async def populate(self, team_range):
        async with AsyncSessionLocal() as session:
            async with session.begin():
                if team_range == "A-M":
                    stmt = select(Team).where(Team.TeamName.ilike("a%") |
                                            Team.TeamName.ilike("b%") |
                                            Team.TeamName.ilike("c%") |
                                            Team.TeamName.ilike("d%") |
                                            Team.TeamName.ilike("e%") |
                                            Team.TeamName.ilike("f%") |
                                            Team.TeamName.ilike("g%") |
                                            Team.TeamName.ilike("h%") |
                                            Team.TeamName.ilike("i%") |
                                            Team.TeamName.ilike("j%") |
                                            Team.TeamName.ilike("k%") |
                                            Team.TeamName.ilike("l%") |
                                            Team.TeamName.ilike("m%")).order_by(Team.TeamName.asc())
                else:  # N-Z
                    stmt = select(Team).where(Team.TeamName.ilike("n%") |
                                            Team.TeamName.ilike("o%") |
                                            Team.TeamName.ilike("p%") |
                                            Team.TeamName.ilike("q%") |
                                            Team.TeamName.ilike("r%") |
                                            Team.TeamName.ilike("s%") |
                                            Team.TeamName.ilike("t%") |
                                            Team.TeamName.ilike("u%") |
                                            Team.TeamName.ilike("v%") |
                                            Team.TeamName.ilike("w%") |
                                            Team.TeamName.ilike("x%") |
                                            Team.TeamName.ilike("y%") |
                                            Team.TeamName.ilike("z%")).order_by(Team.TeamName.asc())

                result = await session.execute(stmt)
                teams = result.scalars().all()
                self.options = [discord.SelectOption(label=team.TeamName, value=str(team.TeamName)) for team in teams]

            
class LeagueDraftView(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.cube_choice = None
        self.your_team_choice = None
        self.opponent_team_choice = None
        self.session_type = "premade"
        self.cube_select = CubeSelect()
        self.your_team_select = TeamSelect("Your Team", "your_team_choice")
        self.opponent_team_select = TeamSelect("Opposing Team", "opponent_team_choice")
        self.add_item(self.cube_select)
        self.add_item(self.your_team_select)
        self.add_item(self.opponent_team_select)

    async def check_and_send_summary(self, interaction: discord.Interaction):
        if self.cube_choice and self.your_team_choice and self.opponent_team_choice:
            await interaction.followup.send("Lobby creation in progress. Standby", ephemeral=True)
            bot = interaction.client
            team_a_name = self.your_team_choice
            team_b_name = self.opponent_team_choice
            
            from session import register_team_to_db
            # Register Team A if not present
            team_a, team_a_msg = await register_team_to_db(team_a_name)
            # Register Team B if not present
            team_b, team_b_msg = await register_team_to_db(team_b_name)

            draft_start_time = datetime.now().timestamp()
            session_id = f"{interaction.user.id}-{int(draft_start_time)}"
            draft_id = ''.join(random.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
            draft_link = f"https://draftmancer.com/?session=DB{draft_id}"
            
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    new_draft_session = DraftSession(
                        session_id=session_id,
                        guild_id=str(interaction.guild_id),
                        draft_link=draft_link,
                        draft_id=draft_id,
                        draft_start_time=datetime.now(),
                        deletion_time=datetime.now() + timedelta(hours=3),
                        session_type="premade",
                        premade_match_id=None,
                        team_a_name=team_a_name,
                        team_b_name=team_b_name,
                        tracked_draft = True,
                        true_skill_draft = False
                    )
                    session.add(new_draft_session)
                    
            # Generate and send the embed message
            embed = discord.Embed(title=f"League Match: {team_a_name} vs. {team_b_name}",
                                  description=f"\n\nDraft Start Time: <t:{int(draft_start_time)}:F> \n\n**How to use bot**:\n1. Click your team name to join that team. Enter the draftmancer link. Draftmancer host still has to update settings and import from CubeCobra.\n" +
                                "2. When all teams are joined, Push Ready Check. Once everyone is ready, push Generate Seating Order\n" +
                                "3. Draftmancer host needs to adjust table to match seating order. **TURN OFF RANDOM SEATING IN DRAFTMANCER** \n" +
                                "4. After the draft, come back to this message (it'll be in pins) and push Create Rooms and Post Pairings.\n" +
                                "5. You will now have a private team chat with just your team and a shared draft-chat that has pairings and match results. You can select the Match Results buttons to report results.\n" +
                                "6. Chat channels will automatically close around five hours after the /leaguedraft command was used." +
                                f"\n\n**Chosen Cube: [{self.cube_choice}](https://cubecobra.com/cube/list/{self.cube_choice})** \n**Draftmancer Session**: **[Join Here]({draft_link})**",
                color=discord.Color.blue()
                )
            embed.add_field(name=f"{team_a_name}", value="No players yet.", inline=False)
            embed.add_field(name=f"{team_b_name}", value="No players yet.", inline=False)
            embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1186757246936424558/1217295353972527176/131.png")
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    new_match = Match(
                        TeamAID=team_a.TeamID,
                        TeamBID=team_b.TeamID,
                        TeamAWins=0,
                        TeamBWins=0,
                        DraftWinnerID=None,
                        MatchDate=datetime.now(),
                        TeamAName=team_a_name,
                        TeamBName=team_b_name
                    )
                    session.add(new_match)
                    await session.commit()
                    match_id = new_match.MatchID
            print(f"League Draft: {session_id} has been created.")
            
            from session import get_draft_session
            draft_session = await get_draft_session(session_id)
            if draft_session:
                from views import PersistentView
                view = PersistentView(
                    bot=bot,
                    draft_session_id=draft_session.session_id,
                    session_type=self.session_type,
                    team_a_name=team_a_name,
                    team_b_name=team_b_name
                )
            message = await interaction.followup.send(embed=embed, view=view)

            if new_draft_session:
                async with AsyncSessionLocal() as session:
                    async with session.begin():
                        result = await session.execute(select(DraftSession).filter_by(session_id=new_draft_session.session_id))
                        updated_session = result.scalars().first()
                        if updated_session:
                            updated_session.message_id = str(message.id)
                            updated_session.draft_channel_id = str(message.channel.id)
                            updated_session.premade_match_id = str(match_id)
                            session.add(updated_session)
                            await session.commit()

            # Pin the message to the channel
            await message.pin()



class InitialPostView(View):
    def __init__(self, command_type=None, team_id=None, team_name=None, user_display_name=None):
        super().__init__()
        self.your_team_range = None
        self.command_type = command_type
        self.team_id = team_id
        self.team_name = team_name
        self.user_display_name = user_display_name
        self.cube_choice = None 
        self.time_zone = None
        if not self.team_id:
            self.add_item(RangeSelect("Your Team Range", "your_team_range"))
        elif self.team_id:
            self.time_zone_select = TimezoneSelect("time_zone")
            self.cube_select = ChallengeCubeSelect("cube_choice")
            self.add_item(self.cube_select)
            self.add_item(self.time_zone_select)
    
    async def check_and_send_team_cube(self, interaction: discord.Interaction):
        if self.your_team_range:
            new_view = PostTeamView(command_type=self.command_type)
            await new_view.your_team_select.populate(self.your_team_range)
            await interaction.followup.send("Choose the team", view=new_view, ephemeral=True)

    async def try_send_modal(self, interaction: discord.Interaction):
        if self.team_name and self.cube_choice and self.time_zone:
            # When both selections are made, send the modal
            await interaction.response.send_modal(ChallengeTimeModal(self.team_name, self.cube_choice, self.time_zone, self.command_type))
        else:
            await interaction.response.defer()
class PostTeamView(View):
    def __init__(self, command_type):
        super().__init__()
        self.team_selection = None  # Holds the selected team name
        #self.cube_selection = None  # Holds the selected cube
        self.time_zone = None
        self.command_type = command_type
        self.your_team_select = PostTeamSelect("Your Team", "team_selection")
        #self.cube_select = ChallengeCubeSelect("cube_selection")
        #self.time_zone_select = TimezoneSelect("time_zone")
        #self.add_item(self.cube_select)
        self.add_item(self.your_team_select)
        #self.add_item(self.time_zone_select)

    async def try_send_modal(self, interaction: discord.Interaction):
        if self.team_selection:
            # When both selections are made, send the modal
            await interaction.response.send_modal(RegisterPlayerModal(self.team_selection, self.command_type))
        else:
            await interaction.response.defer()


class RegisterPlayerModal(Modal):
    def __init__(self, team_selection, command_type, *args, **kwargs):
        self.team_selection = team_selection
        self.command_type = command_type
        super().__init__(title="Register Player with PlayerID", *args, **kwargs)
        # Update the placeholder to reflect the desired format
        self.add_item(InputText(label="Integer", placeholder="UserID", custom_id="register_player"))
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user_id = self.children[0].value 

        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                # Check if the team is already registered in TeamRegistration
                team_registration_stmt = select(TeamRegistration).where(TeamRegistration.TeamName == self.team_selection)
                team_registration_result = await db_session.execute(team_registration_stmt)
                team_registration = team_registration_result.scalars().first()

                if not team_registration:
                    # If not found in TeamRegistration, look up in Team by TeamName
                    team_stmt = select(Team).where(Team.TeamName == self.team_selection)
                    team_result = await db_session.execute(team_stmt)
                    team = team_result.scalars().first()

                    if team:
                        member = interaction.guild.get_member(int(user_id))
                        display_name = member.display_name if member else "Unknown User"
                        # If found, create a new entry in TeamRegistration
                        new_team_registration = TeamRegistration(
                            TeamID=team.TeamID,
                            TeamName=team.TeamName,
                            TeamMembers={user_id: display_name}  # Include the registering user
                        )
                        db_session.add(new_team_registration)
                        await db_session.commit()
                        await interaction.followup.send("Team registration completed successfully.", ephemeral=True)
                    else:
                        # If the team is not found in Team either
                        await interaction.followup.send("Team not found. Please ensure the team name is correct.", ephemeral=True)
                else:
                    # If the team is already registered, update the TeamMembers JSON
                    member = interaction.guild.get_member(int(user_id))
                    display_name = member.display_name if member else "Unknown User"
                    if user_id not in team_registration.TeamMembers:
                        team_registration.TeamMembers[user_id] = display_name
                        db_session.add(team_registration)
                        await db_session.commit()
                        await interaction.followup.send(f"User {display_name} added to {self.team_selection} successfully.", ephemeral=True)
                    else:
                        await interaction.followup.send(f"User {display_name} is already registered in the team {self.team_selection}.", ephemeral=True)
class TimezoneSelect(Select):
    def __init__(self, attribute_name):
        # Manually curated list of common timezones
        options = [
            discord.SelectOption(label="UTC-12:00 International Date Line West", value="Etc/GMT+12"),
            discord.SelectOption(label="UTC-08:00 Pacific Time (US & Canada)", value="America/Los_Angeles"),
            discord.SelectOption(label="UTC-07:00 Mountain Time (US & Canada)", value="America/Denver"),
            discord.SelectOption(label="UTC-06:00 Central Time (US & Canada)", value="America/Chicago"),
            discord.SelectOption(label="UTC-05:00 Eastern Time (US & Canada)", value="America/New_York"),
            discord.SelectOption(label="UTC-04:00 Atlantic Time (Canada)", value="America/Halifax"),
            discord.SelectOption(label="UTC+00:00 Greenwich Mean Time", value="GMT"),
            discord.SelectOption(label="UTC+01:00 Central European Time", value="Europe/Berlin"),
            discord.SelectOption(label="UTC+02:00 Eastern European Time", value="Europe/Athens"),
            discord.SelectOption(label="UTC+03:00 Moscow Time", value="Europe/Moscow"),
            discord.SelectOption(label="UTC+04:00 Gulf Standard Time", value="Asia/Dubai"),
            discord.SelectOption(label="UTC+05:00 Pakistan Standard Time", value="Asia/Karachi"),
            discord.SelectOption(label="UTC+05:30 Indian Standard Time", value="Asia/Kolkata"),
            discord.SelectOption(label="UTC+08:00 China Standard Time", value="Asia/Shanghai"),
            discord.SelectOption(label="UTC+09:00 Japan Standard Time", value="Asia/Tokyo"),
            discord.SelectOption(label="UTC+10:00 Australian Eastern Standard Time", value="Australia/Sydney"),
            discord.SelectOption(label="UTC+12:00 New Zealand Standard Time", value="Pacific/Auckland"),
            discord.SelectOption(label="UTC-03:00 Brasilia Time", value="America/Sao_Paulo"),
            discord.SelectOption(label="UTC+07:00 Indochina Time", value="Asia/Bangkok"),
            discord.SelectOption(label="UTC-02:00 Fernando de Noronha Time", value="America/Noronha"),
        ]

        super().__init__(placeholder="Choose your timezone", min_values=1, max_values=1, options=options)
        self.attribute_name = attribute_name

    async def callback(self, interaction: discord.Interaction):
        setattr(self.view, self.attribute_name ,self.values[0])
        await self.view.try_send_modal(interaction)

class AdjustTimeModal(Modal):
    def __init__(self, match_id, *args, **kwargs):
        self.match_id = match_id
        super().__init__(title="Change the time of your match", *args, **kwargs)
        # Update the placeholder to reflect the desired format
        self.add_item(InputText(label="MM/DD/YYYY HH:MM. Use Local Time & 24HR Clock", placeholder="MM/DD/YYYY HH:MM", custom_id="start_time"))
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                challenge_to_update = await db_session.get(Challenge, self.match_id)
                challenge_to_update.start_time = datetime.strptime(self.children[0].value, "%m/%d/%Y %H:%M")
                await db_session.commit()
            bot = interaction.client
            channel = bot.get_channel(int(challenge_to_update.channel_id))
            message = await channel.fetch_message(int(challenge_to_update.message_id))
            formatted_time = f"<t:{int(challenge_to_update.start_time.timestamp())}:F>"
            updated_embed = discord.Embed(title=f"{challenge_to_update.team_a} v. {challenge_to_update.team_b} is scheduled!" if challenge_to_update.team_b else f"{challenge_to_update.team_a} is looking for a match!", 
                                          description=f"Proposed Time: {formatted_time}\nChosen Cube: {challenge_to_update.cube}", color=discord.Color.gold() if challenge_to_update.team_b else discord.Color.blue())

            await message.edit(embed=updated_embed)

class ChallengeTimeModal(Modal):
    def __init__(self, team_name, cube, time_zone, command_type, *args, **kwargs):
        super().__init__(title="Enter a Date & Time", *args, **kwargs)
        self.team_name = team_name
        self.cube_choice = cube
        self.time_zone = time_zone
        self.command_type = command_type
        # Update the placeholder to reflect the desired format
        self.add_item(InputText(label="MM/DD/YYYY HH:MM. Use Local Time & 24HR Clock", placeholder="MM/DD/YYYY HH:MM", custom_id="start_time"))

    async def callback(self, interaction: discord.Interaction):
        if self.command_type == "post":
            try:
                await interaction.response.defer()
                bot = interaction.client
                async with AsyncSessionLocal() as db_session: 
                    async with db_session.begin():
                        team_stmt = select(Team).where(Team.TeamName == self.team_name)
                        team_update = await db_session.scalar(team_stmt)
                        guild = bot.get_guild(int(interaction.guild_id))
                        challenge_channel = discord.utils.get(guild.text_channels, name="league-challenges")

                        user_time_zone = pytz.timezone(self.time_zone)  # Convert the selected timezone string to a pytz timezone
                        local_time = datetime.strptime(self.children[0].value, "%m/%d/%Y %H:%M")
                        local_dt_with_tz = user_time_zone.localize(local_time)  # Localize the datetime
                        utc_dt = local_dt_with_tz.astimezone(pytz.utc)  # Convert to UTC

                        formatted_time = f"<t:{int(utc_dt.timestamp())}:F>"  # Markdown format for dynamic time display
                        

                        async with AsyncSessionLocal() as session:
                                async with session.begin():
                                    new_challenge = Challenge(
                                        team_a_id = team_update.TeamID,
                                        initial_user=str(interaction.user.id),
                                        guild_id = str(interaction.guild_id),
                                        team_b_id = None,
                                        start_time = utc_dt,
                                        team_a = team_update.TeamName,
                                        team_b = None,
                                        message_id = None,
                                        channel_id = str(challenge_channel),
                                        cube = str(self.cube_choice)

                                    )
                                    session.add(new_challenge)
                                    await db_session.commit()
                        # Post the challenge with the selected team and formatted time
                        embed = discord.Embed(title=f"{self.team_name} is looking for a match!", description=f"Proposed Time: {formatted_time}\nChosen Cube: {self.cube_choice}\nNo Opponent Yet. Sign Up below!", color=discord.Color.blue())

                        view = ChallengeView(new_challenge.id, new_challenge.team_b)
                        
                        message = await challenge_channel.send(embed=embed, view=view)
                        await interaction.followup.send("Challenge posted in league-challenges. Good luck in your match!", ephemeral=True)
                        async with AsyncSessionLocal() as db_session:
                            async with db_session.begin():
                                challenge_to_update = await db_session.get(Challenge, new_challenge.id)
                                challenge_to_update.message_id = str(message.id)
                                challenge_to_update.channel_id = str(message.channel.id)
                                await db_session.commit()

            except ValueError:
                # Handle the case where the date format is incorrect
                await interaction.response.send_message("The date format is incorrect. Please use MM/DD/YYYY HH:MM format.", ephemeral=True)
        elif self.command_type == "find":
            try:
                await interaction.response.defer()
                user_time_zone = pytz.timezone(self.time_zone)  # Convert the selected timezone string to a pytz timezone
                local_time = datetime.strptime(self.children[0].value, "%m/%d/%Y %H:%M")
                local_dt_with_tz = user_time_zone.localize(local_time)  # Localize the datetime
                utc_dt = local_dt_with_tz.astimezone(pytz.utc)  # Convert to UTC
                begin_range = utc_dt - timedelta(hours=2)
                end_range = utc_dt + timedelta(hours=2)
                async with AsyncSessionLocal() as db_session: 
                    async with db_session.begin():
                        range_stmt = select(Challenge).where(Challenge.start_time.between(begin_range, end_range),
                                                             Challenge.team_b == None,
                                                             Challenge.message_id != None
                                                             )
                                                                
                        results = await db_session.execute(range_stmt)
                        challenges = results.scalars().all()

                        if not challenges:
                        # No challenges found within the range
                            await interaction.followup.send("No open challenges found within 2 hours of the selected time. Consider using /post_challenge to open a challenge yourself!", ephemeral=True)
                            return
                        # Construct the link to the original challenge message
                        
                        embed = discord.Embed(title="Open Challenges", description="Here are the open challenges within 2 hours of the selected time:", color=discord.Color.blue())

                        for challenge in challenges:
                            for challenge in challenges:
                                message_link = f"https://discord.com/channels/{challenge.guild_id}/{challenge.channel_id}/{challenge.message_id}"
                                # Mention the initial user who posted the challenge
                                initial_user_mention = f"<@{challenge.initial_user}>"
                                # Format the start time of each challenge to display in the embed
                                time = datetime.strptime(str(challenge.start_time), "%Y-%m-%d %H:%M:%S")
                                utc_zone = pytz.timezone("UTC")
                                start_time = utc_zone.localize(time)
                                formatted_time = f"<t:{int(start_time.timestamp())}:F>"
                                embed.add_field(name=f"Team: {challenge.team_a}", value=f"Time: {formatted_time}\nCube: {challenge.cube}\nPosted by: {initial_user_mention}\n[Sign Up Here!]({message_link})", inline=False)
                        await interaction.followup.send(embed=embed, ephemeral=True)
                        
            except ValueError:
                # Handle the case where the date format is incorrect
                await interaction.response.send_message("The date format is incorrect. Please use MM/DD/YYYY HH:MM format.", ephemeral=True)  

class PostTeamSelect(Select):
    def __init__(self, placeholder, attribute_name):
        self.attribute_name = attribute_name
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=[])
        self.attribute_name = attribute_name

    async def callback(self, interaction: discord.Interaction):
        setattr(self.view, self.attribute_name, self.values[0])
        await self.view.try_send_modal(interaction)

    async def populate(self, team_range):
        async with AsyncSessionLocal() as session:
            async with session.begin():
                if team_range == "A-M":
                    stmt = select(Team).where(Team.TeamName.ilike("a%") |
                                            Team.TeamName.ilike("b%") |
                                            Team.TeamName.ilike("c%") |
                                            Team.TeamName.ilike("d%") |
                                            Team.TeamName.ilike("e%") |
                                            Team.TeamName.ilike("f%") |
                                            Team.TeamName.ilike("g%") |
                                            Team.TeamName.ilike("h%") |
                                            Team.TeamName.ilike("i%") |
                                            Team.TeamName.ilike("j%") |
                                            Team.TeamName.ilike("k%") |
                                            Team.TeamName.ilike("l%") |
                                            Team.TeamName.ilike("m%")).order_by(Team.TeamName.asc())
                else:  # N-Z
                    stmt = select(Team).where(Team.TeamName.ilike("n%") |
                                            Team.TeamName.ilike("o%") |
                                            Team.TeamName.ilike("p%") |
                                            Team.TeamName.ilike("q%") |
                                            Team.TeamName.ilike("r%") |
                                            Team.TeamName.ilike("s%") |
                                            Team.TeamName.ilike("t%") |
                                            Team.TeamName.ilike("u%") |
                                            Team.TeamName.ilike("v%") |
                                            Team.TeamName.ilike("w%") |
                                            Team.TeamName.ilike("x%") |
                                            Team.TeamName.ilike("y%") |
                                            Team.TeamName.ilike("z%")).order_by(Team.TeamName.asc())

                result = await session.execute(stmt)
                teams = result.scalars().all()
                self.options = [discord.SelectOption(label=team.TeamName, value=str(team.TeamName)) for team in teams]


class ChallengeView(View):
    def __init__(self, challenge_id, team_b):
        self.challenge_id = challenge_id
        self.team_b = team_b
        super().__init__(timeout=None)
        # Add the "Sign Up" button on initialization
        self.add_buttons()

    def add_buttons(self):
        self.add_item(self.create_button("Sign Up", "green", f"sign_up_{self.challenge_id}", self.sign_up_callback))
        self.add_item(self.create_button("Change Time", "grey", f"change_time_{self.challenge_id}", self.change_time_callback))
        self.add_item(self.create_button("Open Lobby", "primary", f"open_lobby_{self.challenge_id}", self.open_lobby_callback))
        self.add_item(self.create_button("Remove Challenge Post", "red", f"close_{self.challenge_id}", self.close_challenge_callback))

    def create_button(self, label, style, custom_id, custom_callback, disabled=False):
        style = getattr(discord.ButtonStyle, style)
        from views import CallbackButton
        button = CallbackButton(label=label, style=style, custom_id=custom_id, custom_callback=custom_callback, disabled=disabled)
        return button

    async def sign_up_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.team_b:
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
                        
                        challenge_stmt = select(Challenge).where(Challenge.id == self.challenge_id)
                        challenge_result = await session.execute(challenge_stmt)
                        challenge_to_update = challenge_result.scalars().first()
                        challenge_to_update.team_b_id = team_id
                        challenge_to_update.team_b = team_name
                        challenge_to_update.opponent_user = user_id_str
                        
                        # challenge_to_update = await session.execute(update(Challenge)
                        #                  .where(Challenge.id == self.challenge_id)
                        #                  .values(opponent_user=user_id_str,
                        #                          team_b_id=team_id,
                        #                          team_b=team_name))
                        bot = interaction.client
                        channel = bot.get_channel(int(challenge_to_update.channel_id))
                        message = await channel.fetch_message(int(challenge_to_update.message_id))
                        formatted_time=f"<t:{int(challenge_to_update.start_time.timestamp())}:F>"
                        updated_embed = discord.Embed(title=f"{challenge_to_update.team_a} v. {challenge_to_update.team_b} is scheduled!", description=f"Proposed Time: {formatted_time}\nChosen Cube: {challenge_to_update.cube}", color=discord.Color.gold())
                        
                        await message.edit(embed=updated_embed)
                        await interaction.response.send_message("Your team has signed up!", ephemeral=True)
                        await notify_poster(bot=bot, message_id=challenge_to_update.message_id, guild_id=challenge_to_update.guild_id, 
                                   channel_id=challenge_to_update.channel_id, initial_user_id=challenge_to_update.initial_user, 
                                   opponent_user_id=challenge_to_update.opponent_user, team_a=challenge_to_update.team_a, 
                                   team_b=challenge_to_update.team_b, start_time=challenge_to_update.start_time)
                        await session.commit()
                        await asyncio.create_task(schedule_notification(bot=bot, challenge_id=challenge_to_update.id, guild_id=challenge_to_update.guild_id, 
                                   channel_id=challenge_to_update.channel_id, initial_user_id=challenge_to_update.initial_user, 
                                   opponent_user_id=challenge_to_update.opponent_user, team_a=challenge_to_update.team_a, 
                                   team_b=challenge_to_update.team_b, start_time=challenge_to_update.start_time, message_id=challenge_to_update.message_id))
                       
                    else:
                        await interaction.response.send_message("You are not registered to a team! Contact a Cube Overseer", ephemeral=True)
                
            
        else:
            await interaction.response.send_message("Two Teams are already signed up!")

    async def change_time_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(AdjustTimeModal(self.challenge_id))
        except Exception as e:
            print(f"Error in Team Select callback: {e}")
    
    async def close_challenge_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = interaction.client
        
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                team_stmt = select(Challenge).where(Challenge.id == self.challenge_id)
                challenge = await db_session.scalar(team_stmt)
                if interaction.user.id == int(challenge.initial_user):

                    channel = bot.get_channel(int(challenge.channel_id))
                    message = await channel.fetch_message(int(challenge.message_id))
                    await message.delete()

                    await db_session.delete(challenge)
                else:
                    await interaction.response.send_message("You are not authorized to close this challenge", ephemeral=True)


    async def open_lobby_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                team_stmt = select(Challenge).where(Challenge.id == self.challenge_id)
                challenge = await db_session.scalar(team_stmt)

                if challenge.team_a and challenge.team_b:
                    await interaction.response.defer()

                    await interaction.followup.send("Lobby creation in progress. Bot will post in league-play-draft-room", ephemeral=True)
                    bot = interaction.client
                    guild = bot.get_guild(int(interaction.guild_id))
                    lobby_channel = discord.utils.get(guild.text_channels, name="league-play-draft-room")

                    from session import register_team_to_db
                    # Register Team A if not present
                    await register_team_to_db(challenge.team_a)
                    # Register Team B if not present
                    await register_team_to_db(challenge.team_b)

                    draft_start_time = datetime.now().timestamp()
                    session_id = f"{interaction.user.id}-{int(draft_start_time)}"
                    draft_id = ''.join(random.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
                    draft_link = f"https://draftmancer.com/?session=DB{draft_id}"
                    
                    async with AsyncSessionLocal() as session:
                        async with session.begin():
                            new_draft_session = DraftSession(
                                session_id=session_id,
                                guild_id=str(interaction.guild_id),
                                draft_link=draft_link,
                                draft_id=draft_id,
                                draft_start_time=datetime.now(),
                                deletion_time=datetime.now() + timedelta(hours=3),
                                session_type="premade",
                                premade_match_id=None,
                                team_a_name=challenge.team_a,
                                team_b_name=challenge.team_b,
                                tracked_draft = True,
                                true_skill_draft = False,
                            )
                            session.add(new_draft_session)
                            
                    # Generate and send the embed message
                    embed = discord.Embed(title=f"League Match: {challenge.team_a} vs. {challenge.team_b}",
                                        description=f"\n\nDraft Start Time: <t:{int(draft_start_time)}:F> \n\n**How to use bot**:\n1. Click your team name to join that team. Enter the draftmancer link. Draftmancer host still has to update settings and import from CubeCobra.\n" +
                                        "2. When all teams are joined, Push Ready Check. Once everyone is ready, push Generate Seating Order\n" +
                                        "3. Draftmancer host needs to adjust table to match seating order. **TURN OFF RANDOM SEATING IN DRAFTMANCER** \n" +
                                        "4. After the draft, come back to this message (it'll be in pins) and push Create Rooms and Post Pairings.\n" +
                                        "5. You will now have a private team chat with just your team and a shared draft-chat that has pairings and match results. You can select the Match Results buttons to report results.\n" +
                                        "6. Chat channels will automatically close around five hours after the /leaguedraft command was used." +
                                        f"\n\n**Chosen Cube: [{challenge.cube}](https://cubecobra.com/cube/list/{challenge.cube})** \n**Draftmancer Session**: **[Join Here]({draft_link})**",
                        color=discord.Color.dark_red()
                        )
                    embed.add_field(name=f"{challenge.team_a}", value="No players yet.", inline=False)
                    embed.add_field(name=f"{challenge.team_b}", value="No players yet.", inline=False)
                    embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1186757246936424558/1217295353972527176/131.png")
                    async with AsyncSessionLocal() as session:
                        async with session.begin():
                            new_match = Match(
                                TeamAID=challenge.team_a_id,
                                TeamBID=challenge.team_b_id,
                                TeamAWins=0,
                                TeamBWins=0,
                                DraftWinnerID=None,
                                MatchDate=datetime.now(),
                                TeamAName=challenge.team_a,
                                TeamBName=challenge.team_b
                            )
                            session.add(new_match)
                            await session.commit()
                            match_id = new_match.MatchID
                    print(f"League Draft: {session_id} has been created.")
                    
                    from session import get_draft_session
                    draft_session = await get_draft_session(session_id)
                    if draft_session:
                        from views import PersistentView
                        view = PersistentView(
                            bot=bot,
                            draft_session_id=draft_session.session_id,
                            session_type=draft_session.session_type,
                            team_a_name=challenge.team_a,
                            team_b_name=challenge.team_b
                        )
                    message = await lobby_channel.send(embed=embed, view=view)

                    if new_draft_session:
                        async with AsyncSessionLocal() as session:
                            async with session.begin():
                                result = await session.execute(select(DraftSession).filter_by(session_id=new_draft_session.session_id))
                                updated_session = result.scalars().first()
                                if updated_session:
                                    updated_session.message_id = str(message.id)
                                    updated_session.draft_channel_id = str(message.channel.id)
                                    updated_session.premade_match_id = str(match_id)
                                    session.add(updated_session)
                                    await session.commit()

                    # Pin the message to the channel
                    await message.pin()

class ChallengeCubeSelect(discord.ui.Select):
    def __init__(self, attribute_name):
        options = [
            discord.SelectOption(label="LSVCube", description="Curated by Luis Scott Vargas"),
            discord.SelectOption(label="AlphaFrog", description="Curated by Gavin Thompson"),
            discord.SelectOption(label="mtgovintage", description="Curated by Ryan Spain and Chris Wolf"),
        ]
        super().__init__(placeholder="Choose Cube", min_values=1, max_values=1, options=options)
        self.attribute_name = attribute_name

    async def callback(self, interaction: discord.Interaction):
        setattr(self.view, self.attribute_name, self.values[0])
        await self.view.try_send_modal(interaction)
                 

class OpponentPostView(View):
    def __init__(self, challenge_id):
        super().__init__()
        self.challenge_id = challenge_id
        self.your_team_range = None
        self.add_item(RangeSelect("Your Team Range", "your_team_range"))
    
    async def check_and_send_team_cube(self, interaction: discord.Interaction):
        if self.your_team_range:
            new_view = OpponentTeamView(self.challenge_id)
            await new_view.your_team_select.populate(self.your_team_range)
            await interaction.followup.send("Choose your team", view=new_view, ephemeral=True)

class OpponentTeamView(View):
    def __init__(self, challenge_id):
        super().__init__()
        self.challenge_id = challenge_id
        self.your_team_select = None
        self.your_team_select = OpponentTeamSelect("Your Team", "your_team_choice")
        self.add_item(self.your_team_select)

    async def check_and_update_embed(self, interaction: discord.Interaction, selected_team_name):
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                team_stmt = select(Team).where(Team.TeamName == selected_team_name)
                team_update = await db_session.scalar(team_stmt)

                async with AsyncSessionLocal() as session:
                    challenge_to_update = await db_session.get(Challenge, self.challenge_id)
                    challenge_to_update.team_b = str(selected_team_name)
                    challenge_to_update.team_b_id = team_update.TeamID
                    challenge_to_update.opponent_user = str(interaction.user.id)
                    await db_session.commit()
                bot = interaction.client
                channel = bot.get_channel(int(challenge_to_update.channel_id))
                message = await channel.fetch_message(int(challenge_to_update.message_id))
                formatted_time=f"<t:{int(challenge_to_update.start_time.timestamp())}:F>"
                updated_embed = discord.Embed(title=f"{challenge_to_update.team_a} v. {challenge_to_update.team_b} is scheduled!", description=f"Proposed Time: {formatted_time}\nChosen Cube: {challenge_to_update.cube}", color=discord.Color.gold())
                await message.edit(embed=updated_embed)
                await notify_poster(bot=bot, message_id=challenge_to_update.message_id, guild_id=challenge_to_update.guild_id, 
                                   channel_id=challenge_to_update.channel_id, initial_user_id=challenge_to_update.initial_user, 
                                   opponent_user_id=challenge_to_update.opponent_user, team_a=challenge_to_update.team_a, 
                                   team_b=challenge_to_update.team_b, start_time=challenge_to_update.start_time)
                
                await asyncio.create_task(schedule_notification(bot=bot, challenge_id=challenge_to_update.id, guild_id=challenge_to_update.guild_id, 
                                   channel_id=challenge_to_update.channel_id, initial_user_id=challenge_to_update.initial_user, 
                                   opponent_user_id=challenge_to_update.opponent_user, team_a=challenge_to_update.team_a, 
                                   team_b=challenge_to_update.team_b, start_time=challenge_to_update.start_time, message_id=challenge_to_update.message_id))

                

class OpponentTeamSelect(Select):
    def __init__(self, placeholder, attribute_name):
        self.attribute_name = attribute_name
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=[])


    async def callback(self, interaction: discord.Interaction):
        selected_team_name = self.values[0]
        try:
            setattr(self.view, self.attribute_name, selected_team_name)
            await interaction.response.defer(ephemeral=True)
            await self.view.check_and_update_embed(interaction, selected_team_name) 
        
        except Exception as e:
            print(f"Error in Opponent Select callback: {e}")


    async def populate(self, team_range):
        async with AsyncSessionLocal() as session:
            async with session.begin():
                if team_range == "A-M":
                    stmt = select(Team).where(Team.TeamName.ilike("a%") |
                                            Team.TeamName.ilike("b%") |
                                            Team.TeamName.ilike("c%") |
                                            Team.TeamName.ilike("d%") |
                                            Team.TeamName.ilike("e%") |
                                            Team.TeamName.ilike("f%") |
                                            Team.TeamName.ilike("g%") |
                                            Team.TeamName.ilike("h%") |
                                            Team.TeamName.ilike("i%") |
                                            Team.TeamName.ilike("j%") |
                                            Team.TeamName.ilike("k%") |
                                            Team.TeamName.ilike("l%") |
                                            Team.TeamName.ilike("m%")).order_by(Team.TeamName.asc())
                else:  # N-Z
                    stmt = select(Team).where(Team.TeamName.ilike("n%") |
                                            Team.TeamName.ilike("o%") |
                                            Team.TeamName.ilike("p%") |
                                            Team.TeamName.ilike("q%") |
                                            Team.TeamName.ilike("r%") |
                                            Team.TeamName.ilike("s%") |
                                            Team.TeamName.ilike("t%") |
                                            Team.TeamName.ilike("u%") |
                                            Team.TeamName.ilike("v%") |
                                            Team.TeamName.ilike("w%") |
                                            Team.TeamName.ilike("x%") |
                                            Team.TeamName.ilike("y%") |
                                            Team.TeamName.ilike("z%")).order_by(Team.TeamName.asc())

                result = await session.execute(stmt)
                teams = result.scalars().all()
                self.options = [discord.SelectOption(label=team.TeamName, value=str(team.TeamName)) for team in teams]

async def notify_poster(bot, message_id, guild_id, channel_id, initial_user_id, opponent_user_id, team_a, team_b, start_time):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        print(f"Guild {guild_id} not found")
        return
    channel = discord.utils.get(guild.text_channels, name="league-play-coordination")
    
    if not channel:
        print(f"Channel {channel_id} not found in guild {guild_id}")
        return
    formatted_time = f"<t:{int(start_time.timestamp())}:F>"
    initial_user = await bot.fetch_user(int(initial_user_id))
    opponent_user = await bot.fetch_user(int(opponent_user_id))
    if not initial_user or not opponent_user:
        print(f"Users not found: Initial User ID: {initial_user_id}, Opponent User ID: {opponent_user_id}")
        return
    message_link = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    # Ping the users
    await channel.send(f"{initial_user.mention}, a challenger approaches to take on {team_a}! {opponent_user.mention} and {team_b} have signed up for your match at {formatted_time}. [Open Lobby Here]({message_link}) ")

async def notify_teams(bot, guild_id, channel_id, message_id, initial_user_id, opponent_user_id, team_a, team_b):
    guild = bot.get_guild(int(guild_id))
    if not guild:
        print(f"Guild {guild_id} not found")
        return

    channel = discord.utils.get(guild.text_channels, name="league-play-coordination")
    if not channel:
        print(f"Channel {channel_id} not found in guild {guild_id}")
        return
    
    initial_user = await bot.fetch_user(int(initial_user_id))
    opponent_user = await bot.fetch_user(int(opponent_user_id))
    if not initial_user or not opponent_user:
        print(f"Users not found: Initial User ID: {initial_user_id}, Opponent User ID: {opponent_user_id}")
        return
    message_link = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    # Ping the users with the updated message format
    await channel.send(f"{team_a} vs. {team_b} is scheduled to start in 15 minutes. Gather your teams {initial_user.mention} and {opponent_user.mention}. [Click Here to Open Lobby!]({message_link}")


async def schedule_notification(bot, challenge_id, guild_id, channel_id, initial_user_id, opponent_user_id, team_a, team_b, start_time, message_id):
    utc = pytz.utc

    # Convert start_time to a timezone-aware datetime object in Eastern Time
    start_time_naive = datetime.strptime(str(start_time), "%Y-%m-%d %H:%M:%S")

    # Convert the start time to UTC, as your server's timezone might be different
    start_time_utc = start_time_naive.astimezone(utc)

    # Calculate the delay until 15 minutes before the start time in UTC
    now_utc = datetime.now(utc)
    notification_time_utc = start_time_utc - timedelta(minutes=15)
    delay = (notification_time_utc - now_utc).total_seconds()

    if delay > 0:
        await asyncio.sleep(delay)
        await notify_teams(bot, guild_id, channel_id, message_id, initial_user_id, opponent_user_id, team_a, team_b)
    else:
        print("The scheduled time for notification has already passed.")