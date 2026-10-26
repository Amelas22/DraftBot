"""Server-wide league roles: League Participant and Active.

A tournament player's team role lives in services/tournament_roles.py; these
two are the guild-wide pair that go with it. Granted at sign-up, League
Participant is taken back when the player is on no live team any more. Active
is only ever granted here, never removed.

Discord-facing like tournament_roles: every function takes the guild it
reconciles, and the database is only opened for the one lookup that needs it.
"""
from dataclasses import dataclass, field
from typing import Any, Iterable

import discord
from loguru import logger
from sqlalchemy import select

from config import get_config
from database.db_session import db_session
from models.tournament import Tournament, TournamentParticipant, TournamentTeamMember
from services.tournament_roles import ensure_team_roles
from services.tournament_service import ACTIVE_STATUSES, get_active_tournament


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


def league_participant_name(guild_id: str | int) -> str:
    return get_config(guild_id).get("roles", {}).get("league_participant", "League Participant")


def active_role_name(guild_id: str | int) -> str:
    return get_config(guild_id).get("activity_tracking", {}).get("active_role", "Active")


def league_participant_role(guild: discord.Guild) -> discord.Role | None:
    return _find_role(guild, league_participant_name(guild.id))


def active_role(guild: discord.Guild) -> discord.Role | None:
    return _find_role(guild, active_role_name(guild.id))


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


@dataclass
class RoleSyncPlan:
    """What /tournament sync_roles would change. Team names and user ids."""
    teams_missing_role: list[str] = field(default_factory=list)
    grant: list[str] = field(default_factory=list)
    strip: list[str] = field(default_factory=list)
    ok: bool = True  # False once an applied change was refused by Discord


async def plan_role_sync(guild: discord.Guild) -> RoleSyncPlan:
    """Work out what brings the guild's tournament roles up to date.

    Makes no Discord writes. The database is read in one session that is
    closed before any guild state is inspected.
    """
    async with db_session() as session:
        live = await live_member_ids(session, guild.id)
        tournament = await get_active_tournament(session, guild.id)
        teams: list[tuple[str, str | None]] = []
        if tournament is not None:
            result = await session.execute(
                select(TournamentParticipant.team_name, TournamentParticipant.role_id)
                .where(
                    TournamentParticipant.tournament_id == tournament.id,
                    TournamentParticipant.dropped_at.is_(None),
                )
                .order_by(TournamentParticipant.id)
            )
            teams = [(name, role_id) for name, role_id in result.all()]

    teams_missing_role = [
        name for name, role_id in teams
        if not role_id or guild.get_role(int(role_id)) is None
    ]

    league = league_participant_role(guild)
    active = active_role(guild)
    wanted = [r for r in (league, active) if r is not None]
    grant: list[str] = []
    for user_id in sorted(live):
        member = guild.get_member(int(user_id))
        if member is not None and any(r not in member.roles for r in wanted):
            grant.append(user_id)

    strip: list[str] = []
    if league is not None:
        strip = [str(m.id) for m in league.members if str(m.id) not in live]
    return RoleSyncPlan(teams_missing_role, grant, strip)


async def apply_role_sync(guild: discord.Guild) -> RoleSyncPlan:
    """Make the changes plan_role_sync describes. Returns the plan acted on."""
    plan = await plan_role_sync(guild)
    async with db_session() as session:
        tournament = await get_active_tournament(session, guild.id)
        tournament_id = tournament.id if tournament is not None else None
    ok = True
    if tournament_id is not None:
        created = await ensure_team_roles(guild, tournament_id)
        ok = len(created) >= len(plan.teams_missing_role)
    ok = await grant_league_roles(guild, plan.grant) and ok
    # Liveness is re-checked here: the plan is older than the Discord calls above.
    ok = await release_league_role(guild, plan.strip) and ok
    plan.ok = ok
    return plan


DISCORD_MESSAGE_LIMIT = 2000
SYNC_LIST_LIMIT = 20
REFUSED_NOTE = ("\n⚠️ Couldn't make every change — check that the bot's own role sits above them, "
                "then run this again.")


def _listing(title: str, items: list[str]) -> str:
    shown = ", ".join(items[:SYNC_LIST_LIMIT])
    more = f", and {len(items) - SYNC_LIST_LIMIT} more" if len(items) > SYNC_LIST_LIMIT else ""
    return f"**{title}** ({len(items)}): {shown}{more}" if items else f"**{title}** (0)"


def format_role_sync(plan: RoleSyncPlan, applied: bool) -> str:
    """The operator's reply: counts, then up to 20 of each, within one message."""
    lines = [] if applied else [
        "Dry run — nothing changed. Run again with apply:True to make these changes."]
    titles = (
        ("Team roles created", "Given League Participant / Active", "League Participant removed")
        if applied and plan.ok else
        ("Team roles", "League Participant / Active", "League Participant removal")
        if applied else
        ("Teams without a role", "Members to give League Participant / Active",
         "Members to take League Participant from"))
    lines.append(_listing(titles[0], [f"**{n}**" for n in plan.teams_missing_role]))
    lines.append(_listing(titles[1], [f"<@{u}>" for u in plan.grant]))
    lines.append(_listing(titles[2], [f"<@{u}>" for u in plan.strip]))
    text = "\n".join(lines)[:DISCORD_MESSAGE_LIMIT - len(REFUSED_NOTE)]
    if applied and not plan.ok:
        text += REFUSED_NOTE
    return text
