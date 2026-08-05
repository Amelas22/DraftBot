"""Card lending: multi-entity debt ledger (spec 2026-08-05-card-lending-design)."""
import os
import tempfile

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from database.models_base import Base
from database.db_session import AsyncSessionLocal
from models.debt_ledger import DebtLedger


@pytest_asyncio.fixture
async def test_db():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    AsyncSessionLocal.configure(bind=engine)
    yield engine
    await engine.dispose()
    os.unlink(tmp.name)


@pytest.mark.asyncio
async def test_debt_ledger_rows_carry_optional_card_name(test_db):
    async with AsyncSessionLocal() as session:
        session.add(DebtLedger(
            guild_id="g", player_id="1", counterparty_id="2",
            amount=4, source_type="card_loan", source_id="u1",
            card_name="Lightning Bolt"))
        session.add(DebtLedger(
            guild_id="g", player_id="1", counterparty_id="2",
            amount=-30, source_type="draft", source_id="s1"))
        await session.commit()
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(DebtLedger))).scalars().all()
    by_type = {r.source_type: r for r in rows}
    assert by_type["card_loan"].card_name == "Lightning Bolt"
    assert by_type["draft"].card_name is None
