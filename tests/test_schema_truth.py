"""The models must describe the schema the database actually has.

Alembic's autogenerate diffs the models against the database, so anything a
migration created but nobody mirrored into a model reads as something to DROP.
That is not a tidiness problem: a migration generated for an unrelated change
arrives pre-loaded with destructive operations, and the reviewer has to notice
them in a file they did not write.

It also decides what a FRESH database gets. Tests build their schema with
Base.metadata.create_all, so an index only a migration knows about does not
exist in any test -- and the guarantee it provides is untested precisely where
it would be cheapest to check.
"""
import asyncio
import os
import sqlite3
import tempfile

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

import models  # noqa: F401  -- registers every table on the metadata
from database.models_base import Base


def _fresh_schema():
    """Indexes and columns of a database built the way tests build one."""
    path = tempfile.mktemp(suffix=".db")

    async def build():
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(build())
    con = sqlite3.connect(path)
    try:
        return {
            "wallet_tx": [r[1] for r in con.execute("PRAGMA index_list(wallet_tx)")],
            "mtgo_jobs": [r[1] for r in con.execute("PRAGMA index_list(mtgo_jobs)")],
            "match_results": [r[1] for r in con.execute("PRAGMA table_info(match_results)")],
        }
    finally:
        con.close()
        os.unlink(path)


@pytest.mark.parametrize("index", ["uq_wallet_tx_transfer_legs", "uq_wallet_tx_job_kind"])
def test_a_fresh_database_has_the_wallet_idempotency_guards(index):
    """WalletTx's own docstring promises these: "Both are enforced by unique
    indexes, so double-booking is impossible rather than unlikely." On a
    database built from the models that was not true -- the indexes existed
    only in production, so every idempotency test passed on the application
    lock alone, with the backstop absent exactly where it is cheap to test.
    """
    assert index in _fresh_schema()["wallet_tx"], (
        f"{index} exists in production but not in the models, so no test "
        "database has it and the guarantee it provides is never exercised")


def test_a_fresh_database_has_the_job_status_index():
    assert "ix_mtgo_jobs_status" in _fresh_schema()["mtgo_jobs"]


def test_match_results_has_no_guild_id():
    """Dropped by dropguild01: a match belongs to whatever guild its draft
    session does, and the column was NULL on every row."""
    assert "guild_id" not in _fresh_schema()["match_results"]
