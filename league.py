'''
WIP Code
'''

import discord
#from discord.ext import commands
from discord.ui import Select, View #Modal, InputText
from datetime import datetime, timedelta
from session import AsyncSessionLocal, Team, DraftSession, Match
from sqlalchemy import select
import random


class InitialRangeView(View):
    def __init__(self):
        super().__init__()
        self.your_team_range = None
        self.opponent_team_range = None
        self.add_item(RangeSelect("Your Team Range", "your_team_range"))
        self.add_item(RangeSelect("Opposing Team Range", "opponent_team_range"))

    async def check_and_send_team_cube(self, interaction: discord.Interaction):
        print("entering check and send team cube")
        print(f"team ranges: {self.your_team_range} and {self.opponent_team_range}")
        if self.your_team_range and self.opponent_team_range:
            new_view = LeagueDraftView()
            await new_view.your_team_select.populate(self.your_team_range)
            await new_view.opponent_team_select.populate(self.opponent_team_range)
            await interaction.followup.send("Please select the cube and specific teams:", view=new_view, ephemeral=True)

class RangeSelect(Select):
    def __init__(self, placeholder, attribute_name):
        self.attribute_name = attribute_name
        range_choices = [
            discord.SelectOption(label="Team Name: A-M", value="A-M"),
            discord.SelectOption(label="Team Name: N-Z", value="N-Z"),
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
        print(f"team range: {team_range}")
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
            embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1219018393471025242/1219410709440495746/image.png?ex=660b33b8&is=65f8beb8&hm=b7e40e9b872d8e04dd70a30c5abc15917379f9acb7dce74ca0372105ec98b468&")
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











# class TeamSelectView(discord.ui.View):
#     def __init__(self, teams, *args, **kwargs):
#         super().__init__(timeout=None, *args, **kwargs)
#         # Dynamically add the team select dropdown based on registered teams
#         self.add_item(TeamSelect(teams))

# class OpponentSelectView(discord.ui.View):
#     def __init__(self, teams, *args, **kwargs):
#         super().__init__(timeout=None, *args, **kwargs)
#         # Dynamically add the team select dropdown based on registered teams
#         self.add_item(TeamSelect(teams, is_signup=True))

# class TeamSelect(discord.ui.Select):
#     def __init__(self, teams, is_signup=False):
#         self.is_signup = is_signup
#         options = [
#             discord.SelectOption(label=team.TeamName, value=str(team.TeamName)) 
#             for team in teams
#         ]
#         super().__init__(placeholder="Choose your team", min_values=1, max_values=1, options=options)

#     async def callback(self, interaction: discord.Interaction):
#         if self.is_signup:
#             # If it's for sign-up, do something else, like simply sending a message
#             team_name=self.values[0]
#             return team_name
#         else:
#             await interaction.response.send_modal(ChallengeTimeModal(team_name=self.values[0]))

# class ChallengeTimeModal(Modal):
#     def __init__(self, team_name, *args, **kwargs):
#         super().__init__(title="Schedule Your Match", *args, **kwargs)
#         self.team_name = team_name
#         # Update the placeholder to reflect the desired format
#         self.add_item(InputText(label="Use your local time & 24 hour clock", placeholder="MM/DD/YY HH:MM", custom_id="start_time"))

#     async def callback(self, interaction: discord.Interaction):
#         await interaction.response.defer()
#         async with AsyncSessionLocal() as db_session: 
#             async with db_session.begin():
#                 team_stmt = select(Team).where(Team.TeamName == self.team_name)
#                 team_update = await db_session.scalar(team_stmt)

#                 start_time = datetime.strptime(self.children[0].value, "%m/%d/%y %H:%M")
#                 formatted_time = f"<t:{int(start_time.timestamp())}:F>"  # Markdown format for dynamic time display
#                 # Post the challenge with the selected team and formatted time
#                 embed = discord.Embed(title=f"{self.team_name} is looking for a match!", description=f"Start Time: {formatted_time}\n\nNo Opponent Yet. Sign Up below!", color=discord.Color.blue())
#                 message = await interaction.followup.send(embed=embed)
#                 async with AsyncSessionLocal() as session:
#                         async with session.begin():
#                             new_challenge = Challenge(
#                                 message_id = str(message.id),
#                                 guild_id = str(interaction.guild_id),
#                                 channel_id = str(message.channel.id),
#                                 team_a_id = team_update.TeamID,
#                                 team_b_id = None,
#                                 start_time = start_time,
#                                 team_a = team_update.TeamName,
#                                 team_b = None
#                             )
#                             session.add(new_challenge)
#                             await db_session.commit()
#                 #message.pin()


# class SignUpView(discord.ui.View):
#     def __init__(self, team, start_time, *args, **kwargs):
#         super().__init__(timeout=None, *args, **kwargs)
#         self.team = team
#         self.start_time = start_time

#         self.add_item(discord.ui.Button(label="Sign Up", style=discord.ButtonStyle.green, custom_id=f"{self.start_time}_sign_up_button", callback=self.sign_up))

#     async def sign_up(self, interaction: discord.Interaction, button: discord.ui.Button):
#         message_id = interaction.message.id
#         async with AsyncSessionLocal() as session:
#             async with session.begin():
#                 # Fetch all teams sorted by their name
#                 stmt = select(Team).order_by(Team.TeamName.asc())
#                 result = await session.execute(stmt)
#                 teams = result.scalars().all()

#             # If there are no teams registered
#             if not teams:
#                 await interaction.response.send_message("No teams have been registered yet.", ephemeral=True)
#                 return
#             opponent = interaction.response.send_message("Choose your team to sign up:", view=OpponentSelectView(teams), ephemeral=True)
#             team_stmt = select(Team).where(Team.TeamName == str(opponent))
#             team_result = await session.execute(team_stmt)
#             opposing_team = team_result.scalars().first()

#             challenge_stmt = select(Challenge).where(Challenge.message_id == str(message_id))
#             challenge_result = await session.execute(challenge_stmt)
#             challenge = challenge_result.scalars().first()
            
#             challenge.team_b_id = opposing_team.TeamID
#             challenge.team_b = opponent
            
#             bot = interaction.client
#             guild = bot.get_guild(int(challenge.guild_id))
#             channel = guild.get_channel(int(challenge.channel_id))
#             challenge_message = await channel.fetch_message(int(challenge.message_id))
#             updated_embed = discord.Embed(title=f"{challenge.team_a} v. {challenge.team_b}", description=f"Scheduled Start Time: {challenge.start_time}", color=discord.Color.green())
#             await challenge_message.edit(embed=updated_embed)
