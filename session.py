from sqlalchemy import select, func
import warnings
from database.db_session import AsyncSessionLocal, db_session
# Import models here so they are available for reimporting until deprecated
from models import (
    Match,
    MatchResult,
    PlayerStats,
    PlayerLimit,
    Team,
    TeamRegistration,
    WeeklyLimit,
    Challenge,
    SwissChallenge,
    TeamFinder,
    StakeInfo,
    StakePairing,
    DraftSession
)
# Keep all existing functions
async def get_draft_session(session_id: str):
    """Legacy function - consider using DraftSession.get_by_session_id instead"""
    return await DraftSession.get_by_session_id(session_id)


async def register_team_to_db(team_name: str):
    async with db_session() as session:
        # Normalize the team name for case-insensitive comparison
        normalized_team_name = team_name.strip().lower()
        # Check if the team already exists
        query = select(Team).filter(func.lower(Team.TeamName) == normalized_team_name)
        result = await session.execute(query)
        existing_team = result.scalars().first()

        if existing_team:
            return existing_team, f"Team '{existing_team.TeamName}' is already registered."

        # If not exists, create and register the new team
        new_team = Team(TeamName=team_name)
        session.add(new_team)
        
        return new_team, f"Team '{team_name}' has been registered successfully."

async def remove_team_from_db(ctx, team_name: str):
    # Check if the user has the "cube overseer" role
    if any(role.name == "Cube Overseer" for role in ctx.author.roles):
        async with db_session() as session:
            # Normalize the team name for case-insensitive comparison
            normalized_team_name = team_name.strip().lower()
            # Check if the team exists
            query = select(Team).filter(func.lower(Team.TeamName) == normalized_team_name)
            result = await session.execute(query)
            existing_team = result.scalars().first()

            if not existing_team:
                await ctx.send(f"Team '{team_name}' does not exist.")
                return

            # If exists, delete the team
            await session.delete(existing_team)
            
            return f"Team '{team_name}' has been removed"
    else:
        return "You do not have permission to remove a team. This action requires the 'cube overseer' role."