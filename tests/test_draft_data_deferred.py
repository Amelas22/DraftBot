"""`DraftSession.draft_data` is deferred at the mapper, not per query.

The column holds a whole Draftmancer log -- 0.59 MB on average, 309 MB across
the widest single query in the codebase. Loading it by default made every one of
the ~128 `select(DraftSession)` sites pay for a payload almost none of them read,
and the bulk ones became fatal on a 1.96 GB box: the quiz selector alone
materialized 487 MB of decoded Python per tick, which OOM-killed the bot daily.

Deferring at the mapper inverts the default, so a query pays for the log only by
asking. Two production call sites ask, and both are pinned here.

This matters more than the per-query fixes it replaced: those guarded three
known sites, while this guards every site that exists or will be written.
"""
import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import undefer

from conftest import seed_session
from database.db_session import AsyncSessionLocal
from models import DraftSession

_LOG = {"sessionID": "X", "users": {}, "carddata": {"c0": {"name": "Card0"}}}


def _statements_for(engine):
    captured = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    return captured, lambda: event.remove(
        engine.sync_engine, "before_cursor_execute", _capture)


@pytest.mark.asyncio
async def test_bare_select_does_not_load_the_log(test_db):
    """The contract every future query inherits without having to know it."""
    await seed_session(session_id="s-bare", draft_data=_LOG)

    statements, stop = _statements_for(test_db)
    try:
        async with AsyncSessionLocal() as s:
            row = (await s.execute(
                select(DraftSession).where(DraftSession.session_id == "s-bare")
            )).scalar_one()
            assert "draft_data" in inspect(row).unloaded
    finally:
        stop()

    selects = [q for q in statements if "FROM draft_sessions" in q]
    assert selects, "no query hit draft_sessions"
    assert not any("draft_data" in q for q in selects), (
        "draft_data is still in the emitted SQL:\n" + "\n".join(selects))


@pytest.mark.asyncio
async def test_undefer_still_returns_the_log(test_db):
    """The escape hatch the two real readers use."""
    await seed_session(session_id="s-undefer", draft_data=_LOG)

    async with AsyncSessionLocal() as s:
        row = (await s.execute(
            select(DraftSession)
            .options(undefer(DraftSession.draft_data))
            .where(DraftSession.session_id == "s-undefer")
        )).scalar_one()

        assert "draft_data" not in inspect(row).unloaded
        assert row.draft_data == _LOG


@pytest.mark.asyncio
async def test_writing_the_log_does_not_require_loading_it(test_db):
    """draft_setup_manager assigns this column on a row it did not undefer.

    Assignment must not fault the old value in first -- under async SQLAlchemy
    that would raise MissingGreenlet rather than lazy-load.
    """
    await seed_session(session_id="s-write", draft_data=None)

    async with AsyncSessionLocal() as s:
        row = (await s.execute(
            select(DraftSession).where(DraftSession.session_id == "s-write")
        )).scalar_one()
        row.draft_data = _LOG
        await s.commit()

    async with AsyncSessionLocal() as s:
        row = (await s.execute(
            select(DraftSession)
            .options(undefer(DraftSession.draft_data))
            .where(DraftSession.session_id == "s-write")
        )).scalar_one()
        assert row.draft_data == _LOG


@pytest.mark.asyncio
async def test_publish_draft_log_reads_the_log_against_a_real_database(test_db):
    """The first of the two real readers, exercised end-to-end.

    tests/test_draft_table_publish_gate.py and tests/test_log_capture.py cover
    publish_draft_log with a SimpleNamespace row behind a mocked db_session, so
    the ORM query never runs there. That is exactly the shape of test that would
    miss a deferred-column fault: under async SQLAlchemy, reading a deferred
    attribute raises MissingGreenlet rather than lazy-loading, and it would only
    surface in production. This one uses the real session factory and a real
    deferred row.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from services.draft_setup_manager import DraftSetupManager

    await seed_session(session_id="s-publish", stype="random",
                       draft_data=_LOG, sign_ups={"d1": "Alice"})

    m = DraftSetupManager.__new__(DraftSetupManager)
    m.session_id = "s-publish"
    m.draft_id = "ABC123"
    m.guild_id = "42"
    m.session_type = "random"
    m.logger = MagicMock()
    m.socket_client = MagicMock()
    m.socket_client.connected = False
    m.current_draft_log = None

    seen = {}

    async def _embed(draft_data, table_url=None):
        seen["draft_data"] = draft_data
        return True

    with patch.object(DraftSetupManager, "send_magicprotools_embed",
                      AsyncMock(side_effect=_embed)):
        published = await m.publish_draft_log()

    assert published is True, "publish_draft_log could not read the deferred log"
    assert seen["draft_data"] == _LOG

    # And it marked the row published, proving the later writes still work on
    # rows whose fat column was never loaded.
    async with AsyncSessionLocal() as s:
        row = (await s.execute(
            select(DraftSession).where(DraftSession.session_id == "s-publish")
        )).scalar_one()
        assert row.data_received is True


@pytest.mark.asyncio
async def test_submit_draft_log_lookups_work_without_the_log(test_db):
    """/submit_draft_log matches on links, not on the log itself.

    Its two lookups are the widest DraftSession queries in the codebase --
    find_draft_by_url loads every draft a guild has ever run (2,614 rows /
    201 MB of logs on production) to read one small JSON column and filter in
    Python. Neither reads draft_data, so both are correct against a deferred
    row; this pins that, because cogs/draft_logs_cog.py otherwise has no
    behavioural test coverage and the failure would be silent.
    """
    from sqlalchemy import update

    from cogs.draft_logs_cog import DraftLogsCog

    url = "https://magicprotools.com/draft/show?id=abc"
    await seed_session(session_id="s-logs", guild="g-logs", draft_data=_LOG,
                       matches=[("u1", "u2", "u1", None)])
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession)
                        .where(DraftSession.session_id == "s-logs")
                        .values(magicprotools_links={"u1": {"link": url}}))
        await s.commit()

    cog = DraftLogsCog.__new__(DraftLogsCog)

    async with AsyncSessionLocal() as s:
        by_url, player_id, cube, _ = await cog.find_draft_by_url(s, url, "g-logs")
        assert by_url is not None, "URL lookup failed on a deferred row"
        assert player_id == "u1"
        assert cube == "TestCube"
        assert "draft_data" in inspect(by_url).unloaded

        # The record calculation is the only thing the command does with the row.
        assert await cog.calculate_record_for_draft(s, by_url, "u1") == "1-0"

        recent, got_url, _, _ = await cog.find_recent_draft_for_user(s, "u1", "g-logs")
        assert recent is not None, "recent-draft lookup failed on a deferred row"
        assert got_url == url
        assert "draft_data" in inspect(recent).unloaded
