"""Who may borrow, where the library is not open to everyone.

A communal library lends to whoever is in the servers it serves -- the cards
are the members' own. A rental library stocked by sponsors starts with people they trust, so
that the first losses are conversations rather than write-offs.

Listing anybody is what closes a library: one with no members named lends to
everyone in its servers, and naming one restricts it to the named. The list
follows the LIBRARY, so somebody trusted with a sponsor's cards is trusted
with them in every room it lends into. Only borrowing is
gated. Depositing, /mydeposits and /withdraw stay open, because somebody
contributing cards is not the risk.
"""
import pytest

from database.db_session import AsyncSessionLocal
from models.library_member import LibraryMember
import services.library_access_service as access

pytestmark = pytest.mark.asyncio

OPEN_LIB, INVITE_LIB = "communal", "rental"
ALICE, BOB = "u1", "u2"


async def _invite(player, library=INVITE_LIB):
    async with AsyncSessionLocal() as s:
        s.add(LibraryMember(library_id=library, player_id=player, added_by="test"))
        await s.commit()


async def test_a_library_with_nobody_listed_lends_to_everyone(test_db):
    """Cube Night, unchanged. The communal case must not need any setup."""
    assert await access.may_borrow(OPEN_LIB, ALICE) is True


async def test_naming_one_person_closes_it_to_the_rest(test_db):
    await _invite(ALICE)

    assert await access.may_borrow(INVITE_LIB, ALICE) is True
    assert await access.may_borrow(INVITE_LIB, BOB) is False


async def test_closing_one_library_leaves_the_others_open(test_db):
    """The list is per server, so a rental pilot cannot accidentally lock down
    a communal library sharing the same bot."""
    await _invite(ALICE, library=INVITE_LIB)

    assert await access.may_borrow(OPEN_LIB, BOB) is True


async def test_inviting_twice_is_not_an_error(test_db):
    await access.invite(INVITE_LIB, ALICE, "operator")
    await access.invite(INVITE_LIB, ALICE, "operator")

    assert await access.members(INVITE_LIB) == [ALICE]


async def test_members_are_listed_for_the_operator(test_db):
    await access.invite(INVITE_LIB, BOB, "operator")
    await access.invite(INVITE_LIB, ALICE, "operator")

    assert sorted(await access.members(INVITE_LIB)) == [ALICE, BOB]


# --- a revocation must not be a way to open the library ---------------------

async def test_removing_the_last_member_is_refused(test_db):
    """Empty means open, so removing the final member turned a curated library
    into a public one -- and handed the person just removed the access that was
    being taken away. Nothing about "remove Bob" says "and let everyone in".

    The state is reachable, just not by accident: `open` says it outright.
    """
    await _invite(ALICE)

    assert await access.uninvite(INVITE_LIB, ALICE) == "would_open"
    assert await access.may_borrow(INVITE_LIB, BOB) is False, "still curated"
    assert await access.may_borrow(INVITE_LIB, ALICE) is True, "and still a member"


async def test_removing_someone_who_is_not_the_last_is_fine(test_db):
    await _invite(ALICE)
    await _invite(BOB)

    assert await access.uninvite(INVITE_LIB, BOB) == "removed"
    assert await access.may_borrow(INVITE_LIB, BOB) is False


async def test_removing_somebody_who_was_never_listed_says_so(test_db):
    """It used to report success, so a mistyped id read as a revocation that
    had not happened while the real member kept borrowing."""
    await _invite(ALICE)
    await _invite(BOB)

    assert await access.uninvite(INVITE_LIB, "typo") == "not_listed"


async def test_a_library_can_be_opened_on_purpose(test_db):
    await _invite(ALICE)
    await _invite(BOB)

    assert await access.open_library(INVITE_LIB) == 2
    assert await access.may_borrow(INVITE_LIB, "anyone") is True
