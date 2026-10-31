"""Per-tournament Discord roles, one per team.

Discord-facing on purpose: services/tournament_service.py takes a session and
an rng and never touches Discord, and should not grow a guild parameter. This
module is the sibling of services/crown_roles.py in shape -- it reconciles
guild state, and every function takes the guild it is reconciling.

A role's lifetime is one tournament. The largest real event had 42 teams and a
guild caps at 250 roles, so roles are given back when the tournament ends.
"""
import asyncio
from typing import Any, Iterable, NamedTuple

import discord
from loguru import logger
from sqlalchemy import select

from config import get_config
from database.db_session import db_session
from helpers.permissions import get_manager_role_names
from models.tournament import TournamentParticipant

# Discord's cap on a role name. A platform limit, not a product choice.
DISCORD_ROLE_NAME_LIMIT = 100


class TeamRoleTarget(NamedTuple):
    """Everything create_team_roles needs about one team, detached from the ORM.

    Creating 42 roles is ~160 sequential Discord calls, and a caller must not
    hold a database session open across them. Snapshotting into this lets the
    session close first -- and it is why create_team_roles never touches a
    participant attribute that would lazy-load.
    """
    id: int
    team_name: str
    captain_user_id: str
    roster_user_ids: list[str]


def role_target(participant: Any) -> TeamRoleTarget:
    """Snapshot a TournamentParticipant. Must be called while its session is
    still open: roster_user_ids reads an eager-loaded relationship."""
    return TeamRoleTarget(
        id=participant.id,
        team_name=participant.team_name,
        captain_user_id=participant.captain_user_id,
        roster_user_ids=list(participant.roster_user_ids),
    )


TEAM_ROLE_PREFIX = "Team "


def team_role_name(team_name: str) -> str:
    """The role name for a team: the team's name behind TEAM_ROLE_PREFIX, so it
    can never read as a role the bot looks up by name (a team called "Bot
    Manager" gets "Team Bot Manager"). Clipped to Discord's limit; names are not
    made unique, because two concurrent tournaments may each have an "Alpha"
    and the id -- not the name -- is what we store and delete by."""
    return f"{TEAM_ROLE_PREFIX}{team_name}"[:DISCORD_ROLE_NAME_LIMIT]


def role_name_clash(guild: discord.Guild, team_name: str) -> str | None:
    """The role name a team called ``team_name`` would share, or None.

    Roles are found by NAME across the bot -- a manager name grants bot-manager
    (helpers/permissions.py), ``roles.admin`` sees draft rooms
    (helpers/draft_rooms.py), League Participant / Active are looked up by name.
    The prefix keeps a team role clear of those in practice; this is the check
    that it really is: no case-insensitive match with any role already in the
    guild, nor with one of those configured names even before that role exists.
    """
    # Imported here: league_roles imports this module.
    from services.league_roles import active_role_name, league_participant_name

    wanted = team_role_name(team_name.strip()).strip().casefold()
    reserved = [role.name for role in guild.roles]
    admin = get_config(guild.id).get("roles", {}).get("admin")
    reserved += [*sorted(get_manager_role_names(guild.id)),
                 league_participant_name(guild.id), active_role_name(guild.id),
                 *([admin] if isinstance(admin, str) else [])]
    return next((name for name in reserved if name.strip().casefold() == wanted), None)


async def teams_missing_role(guild: discord.Guild, tournament_id: int) -> list[str]:
    """Names of a tournament's non-dropped teams with no working Discord role:
    none stored, or one the guild no longer has. Participant-id order."""
    async with db_session() as session:
        result = await session.execute(
            select(TournamentParticipant.team_name, TournamentParticipant.role_id)
            .where(
                TournamentParticipant.tournament_id == tournament_id,
                TournamentParticipant.dropped_at.is_(None),
            )
            .order_by(TournamentParticipant.id)
        )
        teams = [(name, role_id) for name, role_id in result.all()]
    return [
        name for name, role_id in teams
        if not role_id or guild.get_role(int(role_id)) is None
    ]


async def clashing_teams(guild: discord.Guild, tournament_id: int) -> list[tuple[str, str]]:
    """(team name, clashing role name) for each non-dropped team of a
    tournament that has no working role and could not be given one for want of
    a free name. A team holding its own role is not a clash with it."""
    clashes = ((name, role_name_clash(guild, name)) for name in await teams_missing_role(guild, tournament_id))
    return [(name, clash) for name, clash in clashes if clash is not None]


async def _assign(guild: discord.Guild, role: discord.Role, user_id: str) -> bool:
    """Give `role` to one member, tolerating a member who has left.

    Returns True if the role was applied, False if it could not be (member
    not in the guild, or Discord refused). create_team_roles ignores this --
    one player is not the operation there: a 42-team start must not fail
    because one person left the guild last week. The roster commands in
    cogs/tournament_commands.py do look at it: for a single-target command
    the one player IS the operation, so a swallowed failure there would
    report success on a roster change that silently did nothing.

    HTTPException (not just its NotFound/Forbidden subclasses) is caught so a
    5xx behaves like any other failure -- returned, not propagated into the
    caller after a database write has already committed.
    """
    member = guild.get_member(int(user_id))
    if member is None:
        logger.info(f"[team-roles] {user_id} is not in the guild; no role assigned")
        return False
    try:
        await member.add_roles(role)
        return True
    except discord.HTTPException as e:
        logger.warning(f"[team-roles] could not give {role} to {user_id}: {e}")
        return False


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


# One lock per participant: a sign-up and /tournament start can both ask for the
# same team's role at once, and without this each would create its own.
_ensure_locks: dict[int, asyncio.Lock] = {}


async def _ensure(guild: discord.Guild, participant_id: int) -> tuple[str | None, bool]:
    """(role id, whether this call created it). The stored id is re-read under
    the lock, so a caller that waited sees the role the first one made."""
    lock = _ensure_locks.setdefault(participant_id, asyncio.Lock())
    async with lock:
        async with db_session() as session:
            participant = await session.get(TournamentParticipant, participant_id)
            if participant is None or participant.dropped_at is not None:
                return None, False
            if participant.role_id and guild.get_role(int(participant.role_id)) is not None:
                return participant.role_id, False
            target = role_target(participant)
        clash = role_name_clash(guild, target.team_name)
        if clash is not None:
            # Only when the guild already has a role called "Team <name>": never
            # hand a team a role that reads as someone else's.
            logger.warning(f"[team-roles] not creating a role for {target.team_name}: "
                           f"the name is taken by role '{clash}'")
            return None, False
        try:
            role_ids = await create_team_roles(guild, [target])
        except discord.HTTPException as e:
            logger.warning(f"[team-roles] could not create a role for {target.team_name}: {e}")
            return None, False
        # Imported here: tournament_service is the heavier module and nothing
        # else in this file needs it.
        from services.tournament_service import store_role_ids
        await store_role_ids(role_ids)
        return role_ids[target.id], True


async def ensure_team_role(guild: discord.Guild, participant_id: int) -> str | None:
    """Return the live role id for a team, creating, assigning and recording
    one when it has none or its recorded role is gone from the guild.

    None if the participant no longer exists, has dropped, its name clashes
    with a role (see role_name_clash), or Discord refused the creation (logged,
    never raised): the callers have already committed the change that needs the
    role, and must not fail because of it.
    """
    return (await _ensure(guild, participant_id))[0]


async def ensure_team_roles(guild: discord.Guild, tournament_id: int) -> dict[int, str]:
    """ensure_team_role for every non-dropped team of a tournament. Returns
    {participant id: role id} for the roles it created. One team's refusal is
    logged and skipped; it does not stop the others."""
    async with db_session() as session:
        result = await session.execute(
            select(TournamentParticipant.id).where(
                TournamentParticipant.tournament_id == tournament_id,
                TournamentParticipant.dropped_at.is_(None),
            )
        )
        participant_ids = list(result.scalars())
    created: dict[int, str] = {}
    for participant_id in participant_ids:
        role_id, was_created = await _ensure(guild, participant_id)
        if was_created and role_id is not None:
            created[participant_id] = role_id
    return created


async def sync_member(guild: discord.Guild, role_id: str | None, user_id: str, *, add: bool) -> bool:
    """Add or remove one member's team role.

    Returns True when the target state was reached, including the two cases
    where there was nothing to do: the tournament has no roles (role_id is
    None -- not started, or predates this feature) or the role itself is
    gone. Returns False only when a role change was actually needed and
    Discord would not make it, so the roster commands can warn the operator
    instead of reporting a silent success.

    On the remove side, a member who has already left the guild returns True
    rather than False: they trivially do not have the role any more, so the
    target state is already reached -- unlike the add side, where a member
    not in the guild is exactly the failure to warn about.
    """
    if not role_id:
        return True
    role = guild.get_role(int(role_id))
    if role is None:
        logger.info(f"[team-roles] role {role_id} is gone; nothing to sync")
        return True
    if add:
        return await _assign(guild, role, user_id)
    member = guild.get_member(int(user_id))
    if member is None:
        return True
    try:
        await member.remove_roles(role)
        return True
    except discord.HTTPException as e:
        # Matches _assign's now-widened catch: unlike create_team_roles,
        # there is no all-or-nothing unwind here to justify letting an
        # unexpected 5xx propagate into the caller (e.g. remove_teammate).
        logger.warning(f"[team-roles] could not take {role} from {user_id}: {e}")
        return False


async def delete_team_roles(guild: discord.Guild, role_ids: Iterable[str | None]) -> set[str]:
    """Delete a tournament's roles. Returns the set of ids that are now gone
    -- both the ones this call actually deleted and the ones already absent
    (someone removed the role by hand, or this is a repeat cleanup).

    Idempotent: a role someone already removed by hand counts as gone. Roles
    outlive the bot process, so this must tolerate a guild that has moved on.

    A role Discord refuses to delete (HTTPException -- its role moved above
    the bot's, a 5xx, ...) is deliberately left OUT of the returned set: the
    caller uses this to decide which role_ids are safe to forget, and an id
    for a still-live role must survive so the role can be found and deleted
    later instead of being stranded against the guild's 250-role cap with
    nothing left pointing at it.
    """
    gone: set[str] = set()
    for role_id in role_ids:
        if not role_id:
            continue
        role = guild.get_role(int(role_id))
        if role is None:
            gone.add(role_id)
            continue
        try:
            await role.delete(reason="tournament completed")
            gone.add(role_id)
        except discord.NotFound:
            gone.add(role_id)
        except discord.HTTPException as e:
            logger.warning(f"[team-roles] could not delete role {role_id}: {e}")
    return gone
