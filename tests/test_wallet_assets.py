"""The wallet ledger's ``asset`` column and its transfer-leg uniqueness guard."""
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.wallet_tx import TIX, WalletTx

GUILD = "g1"


def _receive(source, **kw):
    return WalletTx(guild_id=GUILD, player_id="p1", kind="receive", amount=1,
                    source=source, **kw)


@pytest.mark.asyncio
async def test_rows_default_to_tix(test_db):  # noqa: F811
    async with db_session() as session:
        session.add(_receive("s1"))
        await session.commit()
    async with db_session() as session:
        row = (await session.execute(select(WalletTx))).scalar_one()
    assert row.asset == TIX == "tix"


@pytest.mark.asyncio
async def test_same_source_and_kind_allowed_across_assets(test_db):  # noqa: F811
    async with db_session() as session:
        session.add(_receive("s1"))
        session.add(_receive("s1", asset="other"))
        await session.commit()
    async with db_session() as session:
        rows = (await session.execute(select(WalletTx))).scalars().all()
    assert sorted(r.asset for r in rows) == ["other", "tix"]


@pytest.mark.asyncio
async def test_same_source_kind_and_asset_still_rejected(test_db):  # noqa: F811
    async with db_session() as session:
        session.add(_receive("s1"))
        await session.commit()
    with pytest.raises(IntegrityError):
        async with db_session() as session:
            session.add(_receive("s1"))
            await session.commit()
