"""Registering a team and filling its roster, shared by the slash commands and the
Register a team wizard.

Each function does the whole job behind one user action -- the database writes,
the escrow hold, the board refresh, team roles -- and returns what happened instead
of replying. The Find a team reconcile is left to the caller, to run once its
roster is final. The two front ends word their replies
differently (a command says "try /tournament register again", the wizard says
"press Register a team again"), so the replies stay with them; what they must never
do is disagree about what registering means.
"""
from dataclasses import dataclass, field, replace
from typing import Literal

import discord
from loguru import logger

from database.db_session import db_session
from helpers.display_names import get_display_name
from models.tournament import Tournament, TournamentParticipant
from services import tournament_escrow_service as escrow
from services.tournament_formatter import refresh_boards
from services.league_roles import grant_league_roles
from services.tournament_roles import ensure_team_role, sync_member
from services.tournament_service import add_teammate, other_teams_for_user, register_team


RegisterKind = Literal["free_registered", "free_exists", "paid", "already_paid",
                       "not_captain", "pending", "hold_failed", "refused"]


@dataclass
class RegisterOutcome:
    """What a registration attempt came to.

    ``kind`` is one of:
      free_registered -- a free tournament's team was created.
      free_exists     -- that team was already registered (``captain_id`` names its captain).
      paid            -- the fee is in the prize pool; the team is in.
      already_paid    -- the team was registered and paid before.
      not_captain     -- a pending team someone else captains; only they can pay.
      pending         -- the wallet was short by ``deficit``; the spot is held.
      hold_failed     -- a pending row exists but the escrow hold failed (``error``).
      refused         -- nothing was registered (``error`` says why).

    ``roles_synced`` is False when the team's role or the captain's League
    Participant / Active roles could not be given; the registration stands.
    """
    kind: RegisterKind
    team_name: str = ""
    tournament_name: str = ""
    participant_id: int | None = None
    captain_id: str = ""
    fee: int = 0
    deficit: int = 0
    available: int = 0
    error: str = ""
    roles_synced: bool = True


@dataclass
class RosterAdd:
    """One player put on a roster. ``created`` is False when they were already on it."""
    user_id: str
    created: bool
    also_on: list[str] = field(default_factory=list)
    role_synced: bool = True


async def register_captain(bot: discord.Bot, guild_id: str, tournament_id: int,
                           captain_id: str, team_name: str) -> RegisterOutcome:
    """Register a team (see ``_register_row``) and, for every outcome that left a
    team behind, give it its role and the captain League Participant and Active.
    A role the bot could not give is reported in ``roles_synced``, never raised:
    the registration has already committed."""
    guild = bot.get_guild(int(guild_id))
    out = await _register_row(bot, guild_id, tournament_id, captain_id, team_name)
    if out.participant_id is None or out.kind in ("refused", "not_captain"):
        return out
    if guild is None:
        logger.info(f"Guild {guild_id} not visible; no roles given for team '{out.team_name}'")
        return replace(out, roles_synced=False)
    role_id = await ensure_team_role(guild, out.participant_id)
    league_ok = await grant_league_roles(guild, [out.captain_id])
    return replace(out, roles_synced=role_id is not None and league_ok)


async def _register_row(bot: discord.Bot, guild_id: str, tournament_id: int,
                        captain_id: str, team_name: str) -> RegisterOutcome:
    """Register ``team_name`` with ``captain_id`` as captain, paying a fee from their
    wallet when there is one. The caller has already checked that a paid tournament's
    money stack is up and that the captain is linked to MTGO.

    The caller runs the Find a team reconcile once its roster is final: run here,
    before the roster is filled, it would tell the captain's Find a team teammates
    the captain "registered a different team"."""
    try:
        async with db_session() as session:
            tournament = await session.get(Tournament, tournament_id)
            t_name = tournament.name if tournament is not None else ""
            fee = (tournament.entry_fee or 0) if tournament is not None else 0
            participant, created = await register_team(session, tournament_id, team_name, captain_id)
            p_id, p_name = participant.id, participant.team_name
            p_captain, p_status = participant.captain_user_id, participant.status
    except ValueError as e:
        return RegisterOutcome("refused", error=str(e))

    # Every return below replaces kind; "refused" is only a placeholder.
    base = RegisterOutcome("refused", team_name=p_name, tournament_name=t_name,
                           participant_id=p_id, captain_id=p_captain, fee=fee)

    if fee == 0:
        if not created:
            return replace(base, kind="free_exists")
        logger.info(f"Team '{p_name}' registered for tournament {tournament_id} by {captain_id}")
        await refresh_boards(bot, [tournament_id])
        return replace(base, kind="free_registered")

    if p_status == "paid":
        return replace(base, kind="already_paid")
    # Escrow is paid from the captain's wallet, so only the captain can complete it.
    if captain_id != p_captain:
        return replace(base, kind="not_captain")

    res = await escrow.secure_from_wallet(guild_id, captain_id, p_id, tournament_id,
                                          fee, p_name)
    # Every branch below follows a committed participant row, so the board must show
    # it -- the watchdog only refreshes boards for entries it COMPLETES.
    await refresh_boards(bot, [tournament_id])
    if res.get("done"):
        return replace(base, kind="paid")
    if not res.get("ok"):
        return replace(base, kind="hold_failed", error=str(res.get("error")))
    return replace(base, kind="pending", deficit=int(res["deficit"]),
                   available=int(res.get("available", 0)))


async def add_roster_members(bot: discord.Bot, guild: discord.Guild, participant_id: int,
                             members: list[discord.Member]) -> list[RosterAdd]:
    """Put each member on the team's roster and give them its role.

    Re-adding someone already on the roster is not
    an error -- it re-syncs their role, which is the documented repair for a lost role.
    Raises ValueError (from add_teammate) if the roster can no longer change or a
    member is the team's own captain; nothing is written then.
    """
    adds: list[RosterAdd] = []
    async with db_session() as session:
        participant = await session.get(TournamentParticipant, participant_id)
        if participant is None:
            raise ValueError("That team is no longer registered.")
        t_id = participant.tournament_id
        # Checked up front, not left to add_teammate: its SAVEPOINT commits each row
        # on SQLite, so a refusal mid-list would leave the earlier members added.
        captain = next((m for m in members if str(m.id) == participant.captain_user_id), None)
        if captain is not None:
            raise ValueError(f"<@{captain.id}> is the captain of {participant.team_name} "
                             f"and is already on the team.")
        for member in members:
            _, created = await add_teammate(session, participant, member.id,
                                            get_display_name(member, guild))
            others = await other_teams_for_user(session, t_id, member.id, participant.id)
            adds.append(RosterAdd(str(member.id), created, [p.team_name for p in others]))
            if created:
                logger.info(f"{member.id} added to team '{participant.team_name}' in "
                            f"tournament {t_id}")

    # Outside the session: the board reads the roster back in its own session, and a
    # Discord round-trip here would hold this write session's SQLite connection open.
    if adds:
        await refresh_boards(bot, [t_id])
    # ensure_team_role gives a role it creates to the roster as committed above; a
    # role that already existed still has to reach the members added just now.
    role_id = await ensure_team_role(guild, participant_id)
    for add in adds:
        team_ok = role_id is not None and await sync_member(guild, role_id, add.user_id, add=True)
        league_ok = await grant_league_roles(guild, [add.user_id])
        add.role_synced = team_ok and league_ok
    return adds
