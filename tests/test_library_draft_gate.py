"""Who may borrow from a library that also lends to experienced drafters.

`Library.min_drafts_to_borrow` lets a non-member borrow once they have that
many drafts in the library's servers. A member still gets in at any count, and
a library without a threshold behaves exactly as before: nobody named lends to
everyone, anybody named restricts it to the named.
"""
import pytest

from conftest import a_library
from database.db_session import AsyncSessionLocal
from models.library_server import LibraryServer
from models.player import PlayerStats
import services.library_access_service as access

pytestmark = pytest.mark.asyncio

LIB, GUILD = "lounge", "g1"
MEMBER, PLAYER = "u-member", "u-player"


async def _drafted(player, n, guild=GUILD):
    async with AsyncSessionLocal() as session:
        session.add(PlayerStats(player_id=player, guild_id=guild,
                                display_name=player, drafts_participated=n))
        await session.commit()


async def _gated(threshold=3):
    await a_library(LIB, guild=GUILD, kind="rental", min_drafts=threshold)
    await access.invite(LIB, MEMBER, "test")


@pytest.mark.parametrize("drafts,allowed,why", [
    (3, True, "exactly enough is enough"),
    (2, False, "one short is one short"),
    (0, False, "a row that says they have played nothing"),
])
async def test_the_threshold_decides_for_a_non_member(test_db, drafts, allowed, why):
    await _gated()
    await _drafted(PLAYER, drafts)

    assert await access.may_borrow(LIB, PLAYER) is allowed, why


async def test_a_member_borrows_with_no_drafts_at_all(test_db):
    await _gated()

    assert await access.may_borrow(LIB, MEMBER) is True


async def test_no_history_here_counts_as_none(test_db):
    """No PlayerStats row and a row saying zero must read the same."""
    await _gated()

    assert await access.drafts_played(LIB, PLAYER) == 0
    assert await access.may_borrow(LIB, PLAYER) is False


async def test_drafts_are_summed_across_the_librarys_servers(test_db):
    await _gated()
    async with AsyncSessionLocal() as session:
        session.add(LibraryServer(guild_id="g2", library_id=LIB, bound_by="test"))
        await session.commit()
    await _drafted(PLAYER, 2, guild=GUILD)
    await _drafted(PLAYER, 1, guild="g2")

    assert await access.drafts_played(LIB, PLAYER) == 3
    assert await access.may_borrow(LIB, PLAYER) is True


async def test_drafts_in_a_room_this_library_does_not_serve_do_not_count(test_db):
    await _gated()
    await _drafted(PLAYER, 500, guild="g-elsewhere")

    assert await access.may_borrow(LIB, PLAYER) is False


async def test_without_a_threshold_the_list_alone_decides(test_db):
    """Every library until somebody sets the column."""
    await a_library(LIB, guild=GUILD, kind="rental")
    await access.invite(LIB, MEMBER, "test")
    await _drafted(PLAYER, 500)

    assert await access.may_borrow(LIB, PLAYER) is False
    assert await access.is_invite_only(LIB) is True


async def test_a_gated_library_is_not_invite_only(test_db):
    """So the signup board and the cube dropdown show it again."""
    await _gated()

    assert await access.is_invite_only(LIB) is False


async def test_a_pod_is_answered_in_one_call_by_the_same_rule(test_db):
    await _gated()
    await _drafted("u-three", 3)
    await _drafted("u-two", 2)

    allowed = await access.may_borrow_many(LIB, [MEMBER, "u-three", "u-two", "u-new"])

    assert allowed == {MEMBER, "u-three"}


async def test_the_first_invite_does_not_close_a_gated_library(test_db):
    """invite() returning True is what makes the tool warn that everyone else
    just lost access -- untrue when the threshold still lets them in."""
    await a_library(LIB, guild=GUILD, kind="rental", min_drafts=3)

    assert await access.invite(LIB, MEMBER, "test") is False


async def test_removing_the_last_member_of_a_gated_library_is_allowed(test_db):
    """"would_open" guards against an empty list opening the library to all.
    With a threshold, an empty list only lends to the experienced."""
    await _gated()

    assert await access.uninvite(LIB, MEMBER) == "removed"
    assert await access.may_borrow(LIB, MEMBER) is False


async def test_a_refusal_names_the_threshold_and_their_count(test_db):
    await _gated()
    await _drafted(PLAYER, 2)

    said = await access.why_not(LIB, PLAYER)

    assert "3+" in said and "you have 2" in said, said
