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


@pytest.mark.asyncio
async def test_calculate_and_store_stakes_records_sides(test_db, monkeypatch):
    """The write site has team_a/team_b in scope; it must not discard them."""
    from types import SimpleNamespace
    from services import stake_service
    from session import StakeInfo

    async with AsyncSessionLocal() as session:
        for pid, amt in (("alice", 30), ("charlie", 30)):
            session.add(StakeInfo(session_id="s2", player_id=pid, max_stake=amt))
        await session.commit()

    draft = SimpleNamespace(
        session_id="s2", team_a=["alice"], team_b=["charlie"], min_stake=10,
    )
    monkeypatch.setattr(stake_service, "get_config", lambda gid: {})
    await stake_service.calculate_and_store_stakes("guild_1", draft)

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(StakePairing.__table__.select())).fetchall()

    assert rows, "no pairing was written"
    for r in rows:
        backed = {r.player_a_id: r.side_a, r.player_b_id: r.side_b}
        assert backed.get("alice") == "A"
        assert backed.get("charlie") == "B"
