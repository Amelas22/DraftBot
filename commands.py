import discord
from session import register_team_to_db, Team, AsyncSessionLocal
from sqlalchemy import select


async def league_commands(bot):
    @bot.slash_command(name="registerteam", description="Register a new team in the league")
    async def register_team(interaction: discord.Interaction, team_name: str):
        response = await register_team_to_db(team_name)
        await interaction.response.send_message(response, ephemeral=True)
    
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