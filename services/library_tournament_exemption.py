"""Who a running tournament lets borrow from the library for free.

A tournament with `library_exempt` set lends its players the library's decks on
the house while it is active: no member list or draft count to clear
(library_access_service) and no hold to put up (library_trust_service). Both
ask this module, so the prompt, the hold it quotes, deck assignment and the
collection itself all agree on who is exempt.

Who: the captain and every rostered player of each team that is paid and has
not dropped. A team knocked out at the cut is still in the tournament until it
completes; a team that dropped has left it. Captains are asked separately
because tournament_participants.captain_user_id is the one record of them --
the roster deliberately does not repeat them.

Where: only the libraries the tournament's own server draws on. A tournament
says nothing about anybody else's cards.

When: `active` only. Registration is when rosters change and entries can still
be refunded, so a place on a roster then is not yet a place in the tournament.
"""
from typing import Any, Iterable

from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from models.library_server import LibraryServer
from models.tournament import Tournament, TournamentParticipant, TournamentTeamMember


async def exempt_among(library_id: Any, player_ids: "Iterable[Any]") -> "set[str]":
    """The players among these a running tournament exempts from this library's
    gate and hold. One read for the whole list, since deck assignment asks for a
    pod at once."""
    ids = {str(p) for p in player_ids}
    if not library_id or not ids:
        return set()
    guilds = select(LibraryServer.guild_id).where(
        LibraryServer.library_id == str(library_id))
    teams = (select(TournamentParticipant.id)
             .join(Tournament, Tournament.id == TournamentParticipant.tournament_id)
             .where(Tournament.status == "active",
                    Tournament.library_exempt.is_(True),
                    Tournament.guild_id.in_(guilds),
                    TournamentParticipant.status == "paid",
                    TournamentParticipant.dropped_at.is_(None)))
    async with AsyncSessionLocal() as session:
        captains = (await session.scalars(
            select(TournamentParticipant.captain_user_id).where(
                TournamentParticipant.id.in_(teams),
                TournamentParticipant.captain_user_id.in_(ids)))).all()
        rostered = (await session.scalars(
            select(TournamentTeamMember.user_id).where(
                TournamentTeamMember.participant_id.in_(teams),
                TournamentTeamMember.user_id.in_(ids)))).all()
    return {str(p) for p in (*captains, *rostered)}


async def is_exempt(library_id: Any, player_id: Any) -> bool:
    """Whether a running tournament exempts this player from this library's terms."""
    return str(player_id) in await exempt_among(library_id, [player_id])
