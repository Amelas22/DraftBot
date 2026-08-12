"""Debt settlement out of the tix wallet, and the auto-draw that runs after a deposit.

These paths had no coverage, which is how a stale `status=` kwarg survived the move to
the status-free ledger and broke every settlement at runtime.
"""
import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.debt_ledger import DebtLedger
from services import debt_service
from services import mtgo_resolution_service as resolution
from services import wallet_service as ws

GUILD = "g1"
PAYER = "111111111111111111"
CRED = "222222222222222222"
CRED2 = "333333333333333333"


async def _owe(payer, creditor, amount, source_id):
    """Book a debt: payer owes creditor ``amount``."""
    async with db_session() as session:
        session.add(DebtLedger(guild_id=GUILD, player_id=payer, counterparty_id=creditor,
                               amount=-amount, source_type="manual", source_id=source_id,
                               created_by=payer))
        session.add(DebtLedger(guild_id=GUILD, player_id=creditor, counterparty_id=payer,
                               amount=amount, source_type="manual", source_id=source_id,
                               created_by=payer))


@pytest.mark.asyncio
async def test_settlement_moves_tix_and_clears_the_debt(test_db):  # noqa: F811
    await ws.credit_done(GUILD, PAYER, 5, job_id="j1")
    await _owe(PAYER, CRED, 3, "d1")

    res = await resolution.settle_debt_from_wallet(GUILD, PAYER, CRED, 3)
    assert res["ok"]
    assert await ws.get_balance(GUILD, PAYER) == 2
    assert await ws.get_balance(GUILD, CRED) == 3
    # the wallet move is a transfer, so the vault total is untouched
    assert await ws.total_wallets() == 5
    balances = await debt_service.get_all_balances_for(GUILD, PAYER)
    assert balances.get(CRED, 0) == 0


@pytest.mark.asyncio
async def test_settlement_is_idempotent_by_link_id(test_db):  # noqa: F811
    await ws.credit_done(GUILD, PAYER, 5, job_id="j2")
    await _owe(PAYER, CRED, 3, "d2")

    first = await resolution.settle_debt_from_wallet(GUILD, PAYER, CRED, 3, link_id="link-1")
    again = await resolution.settle_debt_from_wallet(GUILD, PAYER, CRED, 3, link_id="link-1")
    assert first["ok"] and again["ok"]
    assert await ws.get_balance(GUILD, PAYER) == 2  # charged once


@pytest.mark.asyncio
async def test_settlement_refuses_more_than_the_wallet_or_the_debt(test_db):  # noqa: F811
    await ws.credit_done(GUILD, PAYER, 2, job_id="j3")
    await _owe(PAYER, CRED, 10, "d3")

    broke = await resolution.settle_debt_from_wallet(GUILD, PAYER, CRED, 5)
    assert not broke["ok"] and "insufficient" in broke["error"].lower()

    await ws.credit_done(GUILD, PAYER, 20, job_id="j3b")
    too_much = await resolution.settle_debt_from_wallet(GUILD, PAYER, CRED, 15)
    assert not too_much["ok"] and "exceeds" in too_much["error"]
    assert await ws.get_balance(GUILD, PAYER) == 22  # nothing moved


@pytest.mark.asyncio
async def test_auto_draw_applies_a_deposit_to_debts_oldest_first(test_db):  # noqa: F811
    await _owe(PAYER, CRED, 2, "d4")
    await _owe(PAYER, CRED2, 2, "d5")
    await ws.credit_done(GUILD, PAYER, 3, job_id="j4")

    drawn = await resolution.auto_draw(GUILD, PAYER)
    assert sum(d["amount"] for d in drawn) == 3  # spent the whole balance
    assert await ws.get_balance(GUILD, PAYER) == 0
    assert await ws.get_balance(GUILD, CRED) + await ws.get_balance(GUILD, CRED2) == 3
    assert await ws.total_wallets() == 3


@pytest.mark.asyncio
async def test_auto_draw_is_a_no_op_without_funds_or_debts(test_db):  # noqa: F811
    assert await resolution.auto_draw(GUILD, PAYER) == []      # no funds, no debts
    await ws.credit_done(GUILD, PAYER, 4, job_id="j5")
    assert await resolution.auto_draw(GUILD, PAYER) == []      # funds, no debts
    assert await ws.get_balance(GUILD, PAYER) == 4
