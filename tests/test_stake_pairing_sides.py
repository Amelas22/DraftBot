"""stake_pairings records which side each party backed.

Settlement used to re-derive this from roster membership, which cannot express
a backer who is on neither roster. The side is known when the pairing is
written, so it is stored there.
"""
import pytest
from conftest import test_db  # noqa: F401  (fixture)
from session import AsyncSessionLocal, StakePairing


@pytest.mark.asyncio
async def test_a_pairing_stores_the_side_each_player_backed(test_db):
    async with AsyncSessionLocal() as session:
        session.add(StakePairing(
            session_id="s1", player_a_id="alice", player_b_id="charlie",
            amount=30, side_a="A", side_b="B",
        ))
        await session.commit()

    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            StakePairing.__table__.select()
        )).first()

    assert row.side_a == "A"
    assert row.side_b == "B"
