"""Where the draft table page joins the existing publish gate.

The gate already decides when a log becomes public and retries until the embed
actually sends. The page rides along with it: built before the embed (its URL
goes inside), persisted only once the embed is truly sent, and never allowed to
stop the embed going out.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.draft_setup_manager import DraftSetupManager
from test_log_capture import _draft_data, _manager, _mock_db_session

URL = "https://magic-draft-logs.nyc3.digitaloceanspaces.com/drafttable/y.html"


def _session_row(session_type="premade"):
    return SimpleNamespace(session_id="sid", draft_data=_draft_data(),
                           data_received=False, session_type=session_type,
                           drafttable_url=None)


@pytest.mark.asyncio
async def test_a_premade_draft_publishes_a_page_and_records_the_url():
    m = _manager()
    ds = _session_row()
    db_factory, _ = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch("services.draft_table_publisher.publish",
               AsyncMock(return_value=URL)) as publish, \
         patch.object(DraftSetupManager, "send_magicprotools_embed",
                      AsyncMock(return_value=True)) as embed:
        ok = await m.publish_draft_log()

    assert ok is True
    publish.assert_awaited_once()
    embed.assert_awaited_once()
    assert embed.await_args.kwargs["table_url"] == URL
    assert ds.drafttable_url == URL
    assert ds.data_received is True


@pytest.mark.asyncio
async def test_a_staked_draft_does_not_build_a_page():
    """Scope is premade only -- every other type must not even call the publisher."""
    m = _manager()
    ds = _session_row(session_type="staked")
    db_factory, _ = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch("services.draft_table_publisher.publish",
               AsyncMock(side_effect=AssertionError("must not build"))) as publish, \
         patch.object(DraftSetupManager, "send_magicprotools_embed",
                      AsyncMock(return_value=True)) as embed:
        ok = await m.publish_draft_log()

    assert ok is True
    publish.assert_not_awaited()
    assert embed.await_args.kwargs["table_url"] is None
    assert ds.drafttable_url is None


@pytest.mark.asyncio
async def test_a_failed_page_still_sends_the_embed():
    """The embed is the deliverable; the page is an enhancement to it."""
    m = _manager()
    ds = _session_row()
    db_factory, _ = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch("services.draft_table_publisher.publish",
               AsyncMock(return_value=None)), \
         patch.object(DraftSetupManager, "send_magicprotools_embed",
                      AsyncMock(return_value=True)) as embed:
        ok = await m.publish_draft_log()

    assert ok is True
    embed.assert_awaited_once()
    assert ds.data_received is True
    assert ds.drafttable_url is None


@pytest.mark.asyncio
async def test_a_failed_send_persists_neither():
    """data_received stays unset so the reconciler retries the whole publish --
    which rebuilds the page too."""
    m = _manager()
    ds = _session_row()
    db_factory, _ = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch("services.draft_table_publisher.publish",
               AsyncMock(return_value=URL)), \
         patch.object(DraftSetupManager, "send_magicprotools_embed",
                      AsyncMock(return_value=False)):
        ok = await m.publish_draft_log()

    assert ok is False
    assert ds.data_received is False
    assert ds.drafttable_url is None
