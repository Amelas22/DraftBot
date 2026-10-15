# tests/test_library_publish.py
"""Committed library writes publish by themselves -- nobody has to remember to."""
import re
from pathlib import Path

import pytest
from sqlalchemy import select, update

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.debt_ledger import DebtLedger
from models.draft_session import DraftSession
from models.library_cube import LibraryCube
from services import library_events as bus
from services import library_publish


@pytest.fixture
def heard(monkeypatch):
    """What would have been published, by call."""
    got = {"libraries": [], "guilds": [], "all": 0}
    monkeypatch.setattr(bus, "publish", lambda lid: got["libraries"].append(str(lid)))
    monkeypatch.setattr(bus, "publish_for_guild", lambda g: got["guilds"].append(str(g)))
    monkeypatch.setattr(bus, "publish_all",
                        lambda: got.__setitem__("all", got["all"] + 1))
    library_publish.install()
    return got


@pytest.mark.asyncio
async def test_a_committed_loan_publishes_its_library(test_db, heard):
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id="g1", library_id="lib", borrower_id="p1",
                       cards=[], state="assigned"))
        await s.commit()
    assert heard["libraries"] == ["lib"]


@pytest.mark.asyncio
async def test_a_rolled_back_write_publishes_nothing(test_db, heard):
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id="g1", library_id="lib", borrower_id="p1",
                       cards=[], state="assigned"))
        await s.flush()
        await s.rollback()
        await s.commit()      # a later commit in the same session inherits nothing
    assert heard["libraries"] == []


@pytest.mark.asyncio
async def test_a_library_scoped_ledger_row_publishes_its_library(test_db, heard):
    async with AsyncSessionLocal() as s:
        s.add(DebtLedger(guild_id="library:lib", player_id="p", counterparty_id="house",
                         amount=1, source_type="card", card_name="Bolt"))
        s.add(DebtLedger(guild_id="g1", player_id="p", counterparty_id="q",
                         amount=5, source_type="draft"))      # an ordinary debt
        await s.commit()
    assert heard["libraries"] == ["lib"]


@pytest.mark.asyncio
async def test_a_draft_publishes_only_when_a_tracked_column_changes(test_db, heard):
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="d1", guild_id="g1", sign_ups={}))
        await s.commit()
    heard["guilds"].clear()

    async with AsyncSessionLocal() as s:
        d = (await s.execute(select(DraftSession).where(
            DraftSession.session_id == "d1"))).scalar_one()
        d.draft_channel_id = "123"          # untracked
        await s.commit()
    assert heard["guilds"] == []

    async with AsyncSessionLocal() as s:
        d = (await s.execute(select(DraftSession).where(
            DraftSession.session_id == "d1"))).scalar_one()
        d.sign_ups = {"p1": "Ada"}          # tracked
        await s.commit()
    assert heard["guilds"] == ["g1"]


@pytest.mark.asyncio
async def test_a_bulk_update_of_a_tracked_column_publishes_every_library(test_db, heard):
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="d1", guild_id="g1", sign_ups={}))
        await s.commit()
        await s.execute(update(DraftSession).where(DraftSession.session_id == "d1")
                        .values(library_released_at=None, sign_ups={"p": "x"}))
        await s.commit()
    assert heard["all"] == 1


@pytest.mark.asyncio
async def test_a_bulk_update_of_untracked_columns_publishes_nothing(test_db, heard):
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="d1", guild_id="g1", sign_ups={}))
        await s.commit()
        heard["guilds"].clear()
        await s.execute(update(DraftSession).where(DraftSession.session_id == "d1")
                        .values(draft_channel_id="999"))
        await s.commit()
    assert heard["all"] == 0 and heard["guilds"] == []


@pytest.mark.asyncio
async def test_an_offer_publishes_its_library(test_db, heard):
    async with AsyncSessionLocal() as s:
        s.add(LibraryCube(library_id="lib", cube_id="mycube"))
        await s.commit()
    assert heard["libraries"] == ["lib"]


@pytest.mark.asyncio
async def test_a_savepoint_rollback_keeps_the_outer_marks(test_db, heard):
    async with AsyncSessionLocal() as s:
        s.add(CardLoan(guild_id="g1", library_id="lib", borrower_id="p1",
                       cards=[], state="assigned"))
        await s.flush()
        nested = await s.begin_nested()
        s.add(CardLoan(guild_id="g1", library_id="other", borrower_id="p2",
                       cards=[], state="assigned"))
        await s.flush()
        await nested.rollback()
        await s.commit()
    assert "lib" in heard["libraries"]


@pytest.mark.asyncio
async def test_a_released_savepoint_publishes_only_at_the_outer_commit(test_db, heard):
    async with AsyncSessionLocal() as s:
        async with s.begin_nested():
            s.add(CardLoan(guild_id="g1", library_id="lib", borrower_id="p1",
                           cards=[], state="assigned"))
        assert heard["libraries"] == []
        await s.commit()
    assert heard["libraries"] == ["lib"]


_TRACKED = r"(card_loans|debt_ledger|draft_sessions|library|library_cube|library_server|library_member|player_preferences)"
# Its writers run only from the alembic legacy-import migration.
_EXEMPT = {"helpers/legacy_import.py"}
_RAW_WRITE = re.compile(rf"(UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+[\"'`]?{_TRACKED}\b",
                        re.IGNORECASE)


def test_no_bot_code_writes_a_tracked_table_in_raw_sql():
    """Raw SQL bypasses the session hooks, so a board would never hear of it."""
    root = Path(__file__).resolve().parents[1]
    skip = {"alembic", "scripts", "tests", ".claude", "venv", ".venv"}
    offenders = [
        str(p.relative_to(root)) for p in root.rglob("*.py")
        if p.relative_to(root).as_posix() not in _EXEMPT
        and not (set(p.relative_to(root).parts) & skip)
        and _RAW_WRITE.search(p.read_text(errors="ignore"))
    ]
    assert offenders == []
