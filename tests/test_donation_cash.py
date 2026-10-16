"""A loser who paid in cash settles their own donation debt, and only that."""
import asyncio

import pytest
from sqlalchemy import select

from database.db_session import db_session
from models.debt_ledger import DebtLedger
from services import debt_service
from services.donation_service import DONATION_SOURCE, mark_paid_cash

BOT = "999"


async def _owes_donation(player, amount, session_id="d1"):
    await debt_service.create_ledger_entries(
        "g", player, BOT, amount, DONATION_SOURCE, f"donation:{session_id}:{player}")


@pytest.mark.asyncio
async def test_paying_in_cash_settles_the_callers_donation_debt(test_db):
    await _owes_donation("b2", 50)

    assert await mark_paid_cash("g", "b2", BOT) == 50
    assert await debt_service.get_balance_with("g", "b2", BOT) == 0
    async with db_session() as s:
        methods = set((await s.execute(select(DebtLedger.settlement_method).where(
            DebtLedger.source_type == "settlement"))).scalars().all())
    assert methods == {"cash"}


@pytest.mark.asyncio
async def test_it_never_touches_another_players_debt(test_db):
    await _owes_donation("b1", 50)
    await _owes_donation("b2", 50)

    await mark_paid_cash("g", "b2", BOT)

    assert await debt_service.get_balance_with("g", "b1", BOT) == -50


@pytest.mark.asyncio
async def test_it_never_touches_a_debt_that_is_not_a_donation(test_db):
    await _owes_donation("b2", 50)
    await debt_service.create_ledger_entries("g", "b2", BOT, 30, "admin", "x")
    await debt_service.create_ledger_entries("g", "b2", "p9", 20, "admin", "y")

    assert await mark_paid_cash("g", "b2", BOT) == 50
    assert await debt_service.get_balance_with("g", "b2", BOT) == -30
    assert await debt_service.get_balance_with("g", "b2", "p9") == -20


@pytest.mark.asyncio
async def test_a_repeat_with_nothing_outstanding_is_a_no_op(test_db):
    await _owes_donation("b2", 50)

    await mark_paid_cash("g", "b2", BOT)

    assert await mark_paid_cash("g", "b2", BOT) == 0


@pytest.mark.asyncio
async def test_two_marks_at_once_settle_the_debt_once(test_db):
    await _owes_donation("b2", 50)

    settled = await asyncio.gather(mark_paid_cash("g", "b2", BOT),
                                   mark_paid_cash("g", "b2", BOT))

    assert sorted(settled) == [0, 50]
    assert await debt_service.get_balance_with("g", "b2", BOT) == 0
