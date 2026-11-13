import pytest
from sqlalchemy import select

from database.db_session import db_session
from models.escrow_hold import OPEN, EscrowHold
from services import wallet_service


def test_holder_id_is_a_system_account():
    assert wallet_service.escrow_holder_id(12) == "escrow:hold:12"
    assert wallet_service.is_system_account("escrow:hold:12")


@pytest.mark.asyncio
async def test_a_new_hold_is_open(test_db):
    async with db_session() as session:
        session.add(EscrowHold(guild_id="g", funder_id="1", counterparty_id="2",
                               amount=50, note="n"))
        await session.commit()

    async with db_session() as session:
        hold = (await session.execute(select(EscrowHold))).scalar_one()

    assert hold.status == OPEN
    assert hold.decided_by is None
    assert hold.decided_at is None
    assert hold.created_at is not None
