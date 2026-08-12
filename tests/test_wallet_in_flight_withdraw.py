"""A withdraw commits tix to system:in-flight, then either crosses the vault boundary
or transfers them back — no row status, no reserve."""
import pytest

from conftest import test_db  # noqa: F401  (fixture)
from services import wallet_service as ws

GUILD = "g1"
PLAYER = "p1"
OTHER = "p2"
IN_FLIGHT = ws.SYSTEM_IN_FLIGHT


async def _commit(n=2, source="wd:test:1"):
    await ws.pay(GUILD, PLAYER, IN_FLIGHT, n, source=source, notes="withdraw")
    return source


@pytest.mark.asyncio
async def test_committed_tix_leave_the_player_and_cannot_be_respent(test_db):  # noqa: F811
    await ws.credit_done(GUILD, PLAYER, 3, job_id="j1")
    await _commit(2)

    assert await ws.get_balance(GUILD, PLAYER) == 1
    assert await ws.get_balance(GUILD, IN_FLIGHT) == 2
    with pytest.raises(ValueError):
        await ws.pay(GUILD, PLAYER, OTHER, 2, source="respend")
    # the system total is untouched: the tix are still in the vault, just committed
    assert await ws.total_wallets() == 3


@pytest.mark.asyncio
async def test_completed_withdraw_crosses_the_boundary_once(test_db):  # noqa: F811
    await ws.credit_done(GUILD, PLAYER, 3, job_id="j2")
    source = await _commit(2)

    await ws.debit_done(GUILD, IN_FLIGHT, 2, job_id="job-x", source=source)
    assert await ws.get_balance(GUILD, IN_FLIGHT) == 0
    assert await ws.get_balance(GUILD, PLAYER) == 1
    assert await ws.total_wallets() == 1  # 2 tix physically left the vault

    # a replayed poll must not debit twice
    await ws.debit_done(GUILD, IN_FLIGHT, 2, job_id="job-x", source=source)
    assert await ws.total_wallets() == 1


@pytest.mark.asyncio
async def test_failed_withdraw_returns_the_tix(test_db):  # noqa: F811
    await ws.credit_done(GUILD, PLAYER, 3, job_id="j3")
    source = await _commit(2)

    await ws.pay(GUILD, IN_FLIGHT, PLAYER, 2, source=f"return:{source}")
    assert await ws.get_balance(GUILD, PLAYER) == 3
    assert await ws.get_balance(GUILD, IN_FLIGHT) == 0
    assert await ws.total_wallets() == 3  # nothing ever left the vault

    # replayed return is idempotent by source
    await ws.pay(GUILD, IN_FLIGHT, PLAYER, 2, source=f"return:{source}")
    assert await ws.get_balance(GUILD, PLAYER) == 3


@pytest.mark.asyncio
async def test_balance_is_a_plain_sum_of_rows(test_db):  # noqa: F811
    """No status filter anywhere: balance == SUM(amount), reconstructible from the log."""
    await ws.credit_done(GUILD, PLAYER, 5, job_id="j4")
    await ws.pay(GUILD, PLAYER, OTHER, 2, source="s1")
    await ws.pay(GUILD, OTHER, PLAYER, 1, source="s2")
    await ws.debit_done(GUILD, PLAYER, 1, job_id="j5")

    rows = await ws.get_history(GUILD, PLAYER, limit=100)
    assert await ws.get_balance(GUILD, PLAYER) == sum(r.amount for r in rows) == 3


@pytest.mark.asyncio
async def test_system_accounts_are_identifiable(test_db):  # noqa: F811
    assert ws.is_system_account(ws.SYSTEM_IN_FLIGHT)
    assert ws.is_system_account("prize:tourney:7")
    assert not ws.is_system_account("123456789012345678")
