"""Batched balance read used to render pending-team deficits without an N+1."""
import pytest

from conftest import test_db  # noqa: F401  (fixture)
from services import wallet_service as ws

GUILD = "g1"
A = "111111111111111111"
B = "222222222222222222"
C = "333333333333333333"


@pytest.mark.asyncio
async def test_balances_for_returns_each_holder_and_zero_for_unknown(test_db):  # noqa: F811
    await ws.credit_done(GUILD, A, 5, job_id="j-a")
    await ws.credit_done(GUILD, B, 2, job_id="j-b")

    got = await ws.balances_for(GUILD, [A, B, C])

    assert got == {A: 5, B: 2, C: 0}


@pytest.mark.asyncio
async def test_balances_for_is_guild_scoped_and_handles_empty(test_db):  # noqa: F811
    await ws.credit_done(GUILD, A, 5, job_id="j-c")

    assert await ws.balances_for("other-guild", [A]) == {A: 0}
    assert await ws.balances_for(GUILD, []) == {}
    assert await ws.balances_for(GUILD, None) == {}
