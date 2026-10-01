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

from conftest import seed_session
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


def _second_refund(behaviour):
    """Let the first refund through, and make the second one `behaviour`."""
    real = pool._refund_in
    calls = []

    async def refund_in(session, *args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            return await behaviour()
        return await real(session, *args, **kwargs)

    return refund_in


async def _refused():
    return False


async def _raises():
    raise RuntimeError("disk I/O error")


@pytest.mark.asyncio
@pytest.mark.parametrize("behaviour", [_refused, _raises], ids=["refused", "raises"])
async def test_a_failed_refund_moves_no_money_at_all(behaviour):
    before = await pool.contributions("g", "s1")

    with patch.object(pool, "_refund_in", _second_refund(behaviour)):
        with pytest.raises(Exception):
            await pool.match_pool("g", "s1", A, B)

    assert await pool.contributions("g", "s1") == before, \
        "a refund committed before the failure: the pool is half-levelled"


@pytest.mark.asyncio
async def test_a_retry_after_a_failure_levels_the_pool_normally():
    with patch.object(pool, "_refund_in", _second_refund(_refused)):
        with pytest.raises(Exception):
            await pool.match_pool("g", "s1", A, B)

    result = await pool.match_pool("g", "s1", A, B)

    held = await pool.contributions("g", "s1")
    assert result["matched"] == 50
    assert sum(held[p] for p in A) == sum(held[p] for p in B) == 50
