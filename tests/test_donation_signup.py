"""A donation draft escrows nothing, so signing up needs no tix."""
import pytest

from conftest import seed_session, seed_stakes
from services import debt_service, draft_pool_service as pool, stake_funding, wallet_service


@pytest.mark.asyncio
async def test_a_donation_sign_up_charges_nothing(test_db):
    await seed_session("d1", guild="g", stage=None, donation=True)

    result = await pool.set_entry("g", "d1", "p1", 100, "joined")

    assert result == {"ok": True, "deficit": 0}
    assert await wallet_service.get_balance("g", "p1") == 0
    assert await pool.pool_balance("g", "d1") == 0


@pytest.mark.asyncio
async def test_a_staked_sign_up_still_charges(test_db):
    await seed_session("s1", guild="g", stage=None)

    result = await pool.set_entry("g", "s1", "p1", 100, "joined")

    assert result == {"ok": False, "deficit": 100}


@pytest.mark.asyncio
async def test_a_donation_stake_cannot_change_once_teams_form(test_db):
    await seed_session("d1", guild="g", stage="teams", donation=True,
                       teams=(["p1"], ["p2"]))

    result = await pool.set_entry("g", "d1", "p1", 50, "revised")

    assert result["ok"] is False


@pytest.mark.asyncio
async def test_a_broke_player_with_debts_can_still_enter_a_donation_draft(test_db):
    await seed_session("d1", guild="g", stage=None, donation=True)
    await debt_service.create_ledger_entries("g", "p1", "p9", 40, "admin", "x")

    funding = await stake_funding.shortfall("g", "p1", "d1", 100)

    assert funding["gap"] == 0


@pytest.mark.asyncio
async def test_a_donation_queue_still_reserves_its_stake_against_a_staked_draft(test_db):
    """Losing a donation draft creates a real debt, so it is a liability."""
    await seed_session("d1", guild="g", stage=None, donation=True,
                       sign_ups={"p1": "Ann"})
    await seed_stakes("d1", {"p1": (100, False)})
    await seed_session("s1", guild="g", stage=None)

    funding = await stake_funding.shortfall("g", "p1", "s1", 20)

    assert funding["at_risk"] == 100 and funding["gap"] == 120
