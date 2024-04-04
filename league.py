'''
WIP Code
'''

import discord
from discord.ext import commands
from discord.ui import Modal, Select, InputText
from datetime import datetime
from session import AsyncSessionLocal, Challenge, Team
from sqlalchemy import select

class TeamSelectView(discord.ui.View):
    def __init__(self, teams, *args, **kwargs):
        super().__init__(timeout=None, *args, **kwargs)
        # Dynamically add the team select dropdown based on registered teams
        self.add_item(TeamSelect(teams))

class OpponentSelectView(discord.ui.View):
    def __init__(self, teams, *args, **kwargs):
        super().__init__(timeout=None, *args, **kwargs)
        # Dynamically add the team select dropdown based on registered teams
        self.add_item(TeamSelect(teams, is_signup=True))

class TeamSelect(discord.ui.Select):
    def __init__(self, teams, is_signup=False):
        self.is_signup = is_signup
        options = [
            discord.SelectOption(label=team.TeamName, value=str(team.TeamName)) 
            for team in teams
        ]
        super().__init__(placeholder="Choose your team", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.is_signup:
            # If it's for sign-up, do something else, like simply sending a message
            team_name=self.values[0]
            return team_name
        else:
            await interaction.response.send_modal(ChallengeTimeModal(team_name=self.values[0]))

class ChallengeTimeModal(Modal):
    def __init__(self, team_name, *args, **kwargs):
        super().__init__(title="Schedule Your Match", *args, **kwargs)
        self.team_name = team_name
        # Update the placeholder to reflect the desired format
        self.add_item(InputText(label="Use your local time & 24 hour clock", placeholder="MM/DD/YY HH:MM", custom_id="start_time"))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        async with AsyncSessionLocal() as db_session: 
            async with db_session.begin():
                team_stmt = select(Team).where(Team.TeamName == self.team_name)
                team_update = await db_session.scalar(team_stmt)

                start_time = datetime.strptime(self.children[0].value, "%m/%d/%y %H:%M")
                formatted_time = f"<t:{int(start_time.timestamp())}:F>"  # Markdown format for dynamic time display
                # Post the challenge with the selected team and formatted time
                embed = discord.Embed(title=f"{self.team_name} is looking for a match!", description=f"Start Time: {formatted_time}\n\nNo Opponent Yet. Sign Up below!", color=discord.Color.blue())
                message = await interaction.followup.send(embed=embed)
                async with AsyncSessionLocal() as session:
                        async with session.begin():
                            new_challenge = Challenge(
                                message_id = str(message.id),
                                guild_id = str(interaction.guild_id),
                                channel_id = str(message.channel.id),
                                team_a_id = team_update.TeamID,
                                team_b_id = None,
                                start_time = start_time,
                                team_a = team_update.TeamName,
                                team_b = None
                            )
                            session.add(new_challenge)
                            await db_session.commit()
                #message.pin()


class SignUpView(discord.ui.View):
    def __init__(self, team, start_time, *args, **kwargs):
        super().__init__(timeout=None, *args, **kwargs)
        self.team = team
        self.start_time = start_time

        self.add_item(discord.ui.Button(label="Sign Up", style=discord.ButtonStyle.green, custom_id=f"{self.start_time}_sign_up_button", callback=self.sign_up))

    async def sign_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        message_id = interaction.message.id
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
            opponent = interaction.response.send_message("Choose your team to sign up:", view=OpponentSelectView(teams), ephemeral=True)
            team_stmt = select(Team).where(Team.TeamName == str(opponent))
            team_result = await session.execute(team_stmt)
            opposing_team = team_result.scalars().first()

            challenge_stmt = select(Challenge).where(Challenge.message_id == str(message_id))
            challenge_result = await session.execute(challenge_stmt)
            challenge = challenge_result.scalars().first()
            
            challenge.team_b_id = opposing_team.TeamID
            challenge.team_b = opponent
            
            bot = interaction.client
            guild = bot.get_guild(int(challenge.guild_id))
            channel = guild.get_channel(int(challenge.channel_id))
            challenge_message = await channel.fetch_message(int(challenge.message_id))
            updated_embed = discord.Embed(title=f"{challenge.team_a} v. {challenge.team_b}", description=f"Scheduled Start Time: {challenge.start_time}", color=discord.Color.green())
            await challenge_message.edit(embed=updated_embed)
