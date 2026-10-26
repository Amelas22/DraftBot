"""Server-wide league roles: League Participant and Active.

A tournament player's team role lives in services/tournament_roles.py; these
two are the guild-wide pair that go with it. Granted at sign-up, League
Participant is taken back when the player is on no live team any more. Active
is only ever granted here, never removed.

Discord-facing like tournament_roles: every function takes the guild it
reconciles, and the database is only opened for the one lookup that needs it.
"""
from typing import Any, Iterable

import discord
from loguru import logger
from sqlalchemy import select

from config import get_config
from database.db_session import db_session
from models.tournament import Tournament, TournamentParticipant, TournamentTeamMember
from services.tournament_service import ACTIVE_STATUSES


async def live_member_ids(session: Any, guild_id: str | int) -> set[str]:
    """User ids on a live team in this guild: captain or roster of a team that
    has not dropped, in a tournament that is registering or running. A pending
    (unpaid) team counts -- it still holds its place."""
    live_team = (
        TournamentParticipant.dropped_at.is_(None),
        Tournament.guild_id == str(guild_id),
        Tournament.status.in_(ACTIVE_STATUSES),
    )
    captains = await session.execute(
        select(TournamentParticipant.captain_user_id)
        .join(Tournament, Tournament.id == TournamentParticipant.tournament_id)
        .where(*live_team)
    )
    roster = await session.execute(
        select(TournamentTeamMember.user_id)
        .join(TournamentParticipant, TournamentParticipant.id == TournamentTeamMember.participant_id)
        .join(Tournament, Tournament.id == TournamentParticipant.tournament_id)
        .where(*live_team)
    )
    return {str(uid) for uid in captains.scalars()} | {str(uid) for uid in roster.scalars()}


def _find_role(guild: discord.Guild, name: str) -> discord.Role | None:
    role = discord.utils.get(guild.roles, name=name)
    if role is None:
        logger.warning(f"[league-roles] role '{name}' does not exist in guild {guild.id}; skipping it")
    return role


def league_participant_role(guild: discord.Guild) -> discord.Role | None:
    name = get_config(guild.id).get("roles", {}).get("league_participant", "League Participant")
    return _find_role(guild, name)


def active_role(guild: discord.Guild) -> discord.Role | None:
    name = get_config(guild.id).get("activity_tracking", {}).get("active_role", "Active")
    return _find_role(guild, name)


async def grant_league_roles(guild: discord.Guild, user_ids: Iterable[str]) -> bool:
    """Give each user League Participant and Active, skipping roles they hold.

    True iff every needed add succeeded. A user who is not in the guild is a
    failure, as in tournament_roles._assign; a role the guild lacks is skipped.
    """
    roles = [r for r in (league_participant_role(guild), active_role(guild)) if r is not None]
    ok = True
    for user_id in user_ids:
        member = guild.get_member(int(user_id))
        if member is None:
            logger.info(f"[league-roles] {user_id} is not in the guild; no roles granted")
            ok = False
            continue
        for role in roles:
            if role in member.roles:
                continue
            try:
                await member.add_roles(role)
            except discord.HTTPException as e:
                logger.warning(f"[league-roles] could not give {role} to {user_id}: {e}")
                ok = False
    return ok


async def release_league_role(guild: discord.Guild, user_ids: Iterable[str]) -> bool:
    """Take League Participant from each given user who is on no live team.

    The live set is read in its own short session, closed before any Discord
    call. A member who left the guild, or never held the role, is success.
    """
    user_ids = list(user_ids)
    async with db_session() as session:
        live = await live_member_ids(session, guild.id)
    role = league_participant_role(guild)
    if role is None:
        return True
    ok = True
    for user_id in user_ids:
        if str(user_id) in live:
            continue
        member = guild.get_member(int(user_id))
        if member is None or role not in member.roles:
            continue
        try:
            await member.remove_roles(role)
        except discord.HTTPException as e:
            logger.warning(f"[league-roles] could not take {role} from {user_id}: {e}")
            ok = False
    return ok
