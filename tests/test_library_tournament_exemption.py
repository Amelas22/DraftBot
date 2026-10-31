"""A tournament can let its players borrow from the library for free.

`Tournament.library_exempt` switches it on per tournament. While that tournament
is running, every captain and rostered player of a paid team still in it may
borrow from any library its server draws on without being on the member list or
having the drafts the threshold asks for, and puts up no hold. Every other
borrower, and every other library, is untouched.
"""
from datetime import datetime

import pytest

from conftest import a_library
from database.db_session import AsyncSessionLocal
from models.tournament import Tournament, TournamentParticipant, TournamentTeamMember
import services.library_access_service as access
import services.library_trust_service as trust
from services.library_service import library_for

pytestmark = pytest.mark.asyncio

LIB, GUILD = "lounge", "g1"
CAPTAIN, ROSTERED, OUTSIDER = "u-captain", "u-rostered", "u-outsider"


async def _lounge():
    """A gated, tiered rental library: invite or 3 drafts to borrow, 50/100 hold."""
    await a_library(LIB, guild=GUILD, kind="rental", collateral=50,
                    collateral_untrusted=100, min_drafts=3)
    await access.invite(LIB, "u-member", "test")
    return await library_for(GUILD)


async def _tournament(*, status="active", exempt=True, guild=GUILD,
                      team_status="paid", dropped=False) -> int:
    """One tournament with one team: CAPTAIN leading, ROSTERED on the roster."""
    async with AsyncSessionLocal() as s:
        t = Tournament(guild_id=guild, name="Season 2", total_rounds=7,
                       status=status, library_exempt=exempt)
        s.add(t)
        await s.flush()
        team = TournamentParticipant(
            tournament_id=t.id, team_id=1, team_name="Team", captain_user_id=CAPTAIN,
            status=team_status, dropped_at=datetime.now() if dropped else None)
        s.add(team)
        await s.flush()
        s.add(TournamentTeamMember(participant_id=team.id, user_id=ROSTERED,
                                   display_name=ROSTERED))
        await s.commit()
        return int(t.id)


async def _free(library, player) -> bool:
    """Borrows without the gate AND without a hold -- the exemption, whole."""
    return (await access.may_borrow(LIB, player)
            and await trust.hold_for(library, player) == 0)


@pytest.mark.parametrize("player", [CAPTAIN, ROSTERED])
async def test_a_running_exempt_tournaments_players_borrow_free(test_db, player):
    library = await _lounge()
    await _tournament()

    assert await _free(library, player)


async def test_somebody_not_in_it_keeps_the_librarys_terms(test_db):
    library = await _lounge()
    await _tournament()

    assert await access.may_borrow(LIB, OUTSIDER) is False
    assert await trust.hold_for(library, OUTSIDER) == 100


@pytest.mark.parametrize("kwargs,why", [
    ({"exempt": False}, "the switch is per tournament, and off unless set"),
    ({"status": "registration"}, "it starts when the tournament does"),
    ({"status": "completed"}, "and ends when it is over"),
    ({"dropped": True}, "a team that left the tournament is not in it"),
    ({"team_status": "pending"}, "an entry that is not paid is not in it"),
    ({"guild": "elsewhere"}, "another server's tournament says nothing about this library"),
])
async def test_no_exemption(test_db, kwargs, why):
    library = await _lounge()
    await _tournament(**kwargs)

    assert await access.may_borrow(LIB, ROSTERED) is False, why
    assert await trust.hold_for(library, ROSTERED) == 100, why


async def test_a_whole_pod_is_sorted_in_one_ask(test_db):
    """Deck assignment asks for everybody at once and must get the same answer
    /library borrow would give each of them."""
    await _lounge()
    await _tournament()

    allowed = await access.may_borrow_many(LIB, [CAPTAIN, ROSTERED, OUTSIDER, "u-member"])

    assert allowed == {CAPTAIN, ROSTERED, "u-member"}


async def test_a_library_that_lends_to_nobody_named_is_unchanged(test_db):
    """The communal case needs no exemption and must not be disturbed by one."""
    await a_library(LIB, guild=GUILD)
    await _tournament()

    assert await access.may_borrow(LIB, OUTSIDER) is True
    assert await trust.hold_for(await library_for(GUILD), OUTSIDER) == 0
