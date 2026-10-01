"""Matching a draft's pool moves all of its refunds, or none of them.

match_pool used to commit each refund on its own, so a failure partway --
a refund refused or raising, or the process dying -- left some players
refunded and the rest not: a pool half-levelled, at stage 'teams', with no
path back. Now every refund is planned first and the lot is applied in one
transaction, so a failure leaves the pool exactly as it was.
"""
from unittest.mock import patch

import pytest
import pytest_asyncio

from conftest import failing_refund, seed_session
from services import draft_pool_service as pool
from services import wallet_service

A = ["a1", "a2"]
B = ["b1", "b2"]
ENTRIES = {"a1": 100, "a2": 50, "b1": 30, "b2": 20}   # A must give back 100: two refunds


@pytest_asyncio.fixture(autouse=True)
async def _a_funded_queue(test_db):
    await seed_session("s1", guild="g", stype="staked", stage=None, teams=(A, B))
    for player, amount in ENTRIES.items():
        await wallet_service.adjust("g", player, 1000, "seed", "test")
        await pool.set_entry("g", "s1", player, amount)


@pytest.mark.asyncio
@pytest.mark.parametrize("behaviour", ["refused", "raises"])
async def test_a_failed_refund_moves_no_money_at_all(behaviour):
    before = await pool.contributions("g", "s1")

    with patch.object(pool, "_refund_in", failing_refund(behaviour=behaviour)):
        with pytest.raises(Exception):
            await pool.match_pool("g", "s1", A, B)

    assert await pool.contributions("g", "s1") == before, \
        "a refund committed before the failure: the pool is half-levelled"


@pytest.mark.asyncio
async def test_a_retry_after_a_failure_levels_the_pool_normally():
    with patch.object(pool, "_refund_in", failing_refund()):
        with pytest.raises(Exception):
            await pool.match_pool("g", "s1", A, B)

    result = await pool.match_pool("g", "s1", A, B)

    held = await pool.contributions("g", "s1")
    assert result["matched"] == 50
    assert sum(held[p] for p in A) == sum(held[p] for p in B) == 50


@pytest.mark.asyncio
async def test_a_failed_match_does_not_record_the_pool_as_matched():
    from session import get_draft_session

    with patch.object(pool, "_refund_in", failing_refund()):
        with pytest.raises(Exception):
            await pool.match_pool("g", "s1", A, B)

    assert (await get_draft_session("s1")).pool_matched_at is None
