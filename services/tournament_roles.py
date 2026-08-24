"""Per-tournament Discord roles, one per team.

Discord-facing on purpose: services/tournament_service.py takes a session and
an rng and never touches Discord, and should not grow a guild parameter. This
module is the sibling of services/crown_roles.py in shape -- it reconciles
guild state, and every function takes the guild it is reconciling.

A role's lifetime is one tournament. The largest real event had 42 teams and a
guild caps at 250 roles, so roles are given back when the tournament ends.
"""
from typing import Any, Iterable

import discord
from loguru import logger

# Discord's cap on a role name. A platform limit, not a product choice.
DISCORD_ROLE_NAME_LIMIT = 100


def team_role_name(team_name: str) -> str:
    """The role name for a team. Clipped to Discord's limit; names are not
    made unique, because two concurrent tournaments may each have an "Alpha"
    and the id -- not the name -- is what we store and delete by."""
    return team_name[:DISCORD_ROLE_NAME_LIMIT]


async def _assign(guild: discord.Guild, role: discord.Role, user_id: str) -> None:
    """Give `role` to one member, tolerating a member who has left.

    NotFound/Forbidden here is one player, not the operation: a 42-team start
    must not fail because one person left the guild last week.
    """
    member = guild.get_member(int(user_id))
    if member is None:
        logger.info(f"[team-roles] {user_id} is not in the guild; no role assigned")
        return
    try:
        await member.add_roles(role)
    except (discord.NotFound, discord.Forbidden) as e:
        logger.warning(f"[team-roles] could not give {role} to {user_id}: {e}")


async def create_team_roles(guild: discord.Guild, participants: Iterable[Any]) -> dict[int, str]:
    """Create one role per participant and give it to captain and roster.

    Returns {participant id: role id}. ALL OR NOTHING: on any failure the roles
    already created are deleted and the error re-raised. Discord has no
    transaction, so the caller's database rollback would otherwise leave real
    roles behind with nothing recording them.

    Each participant must expose `.id`, `.team_name`, `.captain_user_id`, and
    `.roster_user_ids` -- a list of the roster's user ids as strings.
    """
    created: list[Any] = []
    role_ids: dict[int, str] = {}
    try:
        for participant in participants:
            role = await guild.create_role(
                name=team_role_name(participant.team_name),
                mentionable=True,          # a role nobody can mention is pointless here
                reason=f"tournament team {participant.team_name}",
            )
            created.append(role)
            role_ids[participant.id] = str(role.id)
            for user_id in [participant.captain_user_id, *participant.roster_user_ids]:
                await _assign(guild, role, user_id)
    except Exception:
        for role in created:
            try:
                await role.delete(reason="rolling back a failed tournament start")
            except discord.HTTPException as e:
                logger.warning(f"[team-roles] could not roll back {role}: {e}")
        raise
    return role_ids


async def sync_member(guild: discord.Guild, role_id: str | None, user_id: str, *, add: bool) -> None:
    """Add or remove one member's team role. A no-op when the tournament has no
    roles (role_id is None) or the role is gone."""
    if not role_id:
        return
    role = guild.get_role(int(role_id))
    if role is None:
        logger.info(f"[team-roles] role {role_id} is gone; nothing to sync")
        return
    if add:
        await _assign(guild, role, user_id)
        return
    member = guild.get_member(int(user_id))
    if member is None:
        return
    try:
        await member.remove_roles(role)
    except (discord.NotFound, discord.Forbidden) as e:
        logger.warning(f"[team-roles] could not take {role} from {user_id}: {e}")


async def delete_team_roles(guild: discord.Guild, role_ids: Iterable[str | None]) -> int:
    """Delete a tournament's roles. Returns how many were actually deleted.

    Idempotent: a role someone already removed by hand counts as done. Roles
    outlive the bot process, so this must tolerate a guild that has moved on.
    """
    deleted = 0
    for role_id in role_ids:
        if not role_id:
            continue
        role = guild.get_role(int(role_id))
        if role is None:
            continue
        try:
            await role.delete(reason="tournament completed")
            deleted += 1
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            logger.warning(f"[team-roles] could not delete role {role_id}: {e}")
    return deleted
