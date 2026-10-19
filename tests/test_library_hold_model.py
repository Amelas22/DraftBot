from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database.db_session import AsyncSessionLocal
from models.library_hold import LibraryHold


def _server_hold(**overrides):
    start = datetime(2026, 10, 18, 12, 0, 0)
    fields = dict(
        library_id="lib1", cube_id="powerlsv", kind="server", unit="copy",
        starts_at=start, ends_at=start + timedelta(hours=4),
        draft_session_id="draft-1",
    )
    fields.update(overrides)
    return LibraryHold(**fields)


@pytest.mark.asyncio
async def test_a_hold_round_trips(test_db):
    start = datetime(2026, 10, 18, 12, 0, 0)
    async with AsyncSessionLocal() as session:
        session.add(LibraryHold(
            library_id="lib1", cube_id="powerlsv", kind="server", unit="copy",
            cards=[{"name": "Swamp", "qty": 7}], starts_at=start,
            ends_at=start + timedelta(hours=4), draft_session_id="draft-1",
            tournament_match_id=12, player_id="42",
            released_at=start + timedelta(hours=1), released_by="99",
        ))
        await session.commit()
    async with AsyncSessionLocal() as session:
        hold = (await session.execute(select(LibraryHold))).scalar_one()
    assert hold.id is not None
    assert hold.library_id == "lib1"
    assert hold.cube_id == "powerlsv"
    assert hold.kind == "server"
    assert hold.unit == "copy"
    assert hold.cards == [{"name": "Swamp", "qty": 7}]
    assert hold.starts_at == start
    assert hold.ends_at == start + timedelta(hours=4)
    assert hold.draft_session_id == "draft-1"
    assert hold.tournament_match_id == 12
    assert hold.player_id == "42"
    assert hold.released_at == start + timedelta(hours=1)
    assert hold.released_by == "99"
    assert hold.created_at is not None


@pytest.mark.asyncio
async def test_only_one_live_server_hold_per_draft(test_db):
    async with AsyncSessionLocal() as session:
        session.add(_server_hold())
        await session.commit()

    async with AsyncSessionLocal() as session:
        session.add(_server_hold())
        with pytest.raises(IntegrityError):
            await session.commit()

    async with AsyncSessionLocal() as session:
        first = (await session.execute(select(LibraryHold))).scalar_one()
        first.released_at = datetime(2026, 10, 18, 13, 0, 0)
        await session.commit()

    async with AsyncSessionLocal() as session:
        session.add(_server_hold())
        await session.commit()
