"""A withdraw the serve answered oddly must not hand the tix back.

start_withdraw commits the whole amount to in-flight before asking the serve,
and gives it back when no job can be booked. That is right for a REFUSAL --
nothing was opened, so nothing will move. It is wrong for a response we simply
could not read: a split whose parts do not add up still describes trades the
serve has opened, and refunding then pays the player while MTGO moves their tix
anyway.

The two look alike from the caller's side (`jobs_from` returns [] for both) and
they are not alike at all. Told apart by whether the serve answered: a falsy
response never opened anything; a truthy one we could not book may have.
"""
from unittest.mock import AsyncMock, patch

import pytest

from conftest import test_db  # noqa: F401  (fixture)
from services import mtgo_resolution_service as resolution
from services import wallet_service

GUILD, PLAYER, MTGO = "g1", "p1", "Someone"


async def _fund(n):
    from database.db_session import db_session
    async with db_session() as s:
        await wallet_service.transfer_in(s, GUILD, "system:seed", PLAYER, n,
                                         "seed", notes="opening")


async def _balance():
    from database.db_session import db_session
    async with db_session() as s:
        return await wallet_service.balance_in(s, GUILD, PLAYER)


@pytest.mark.asyncio
async def test_a_split_we_cannot_book_does_not_refund(test_db):  # noqa: F811
    """The parts do not sum to the order, so we cannot attribute the tix to
    jobs -- but those jobs exist and are moving them."""
    await _fund(8)
    unbookable = {"batched": True, "batches": [{"id": "a", "give": [{"qty": 3}]}]}

    with patch("services.mtgo_resolution_service.get_client") as gc:
        gc.return_value = AsyncMock(enabled=True)
        gc.return_value.withdraw_tix = AsyncMock(return_value=unbookable)
        res = await resolution.start_withdraw(GUILD, PLAYER, MTGO, 8)

    assert res["ok"] is False
    assert await _balance() == 0, \
        "the tix stay in flight: trades may be open and moving them"


@pytest.mark.asyncio
async def test_a_refusal_still_gives_the_tix_back(test_db):  # noqa: F811
    """The other half of the same branch: the serve never answered, so nothing
    was opened and the player must not be left short."""
    await _fund(8)

    with patch("services.mtgo_resolution_service.get_client") as gc:
        gc.return_value = AsyncMock(enabled=True)
        gc.return_value.withdraw_tix = AsyncMock(return_value=None)
        res = await resolution.start_withdraw(GUILD, PLAYER, MTGO, 8)

    assert res["ok"] is False
    assert await _balance() == 8, "a refusal must return the tix"
