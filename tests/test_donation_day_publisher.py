from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.donation_day_publisher as publisher

EVENT = {"name": "DD", "goal_usd": 100, "starts_at": "2026-10-16T12:00",
         "page_key": "dd", "active": True}


def test_the_page_carries_the_payload_and_escapes_script_breakers():
    html = publisher.render({"name": "</script><b>x", "rows": []})

    assert '<script id="donation-data" type="application/json">' in html
    assert "</script><b>x" not in html


@pytest.mark.asyncio
async def test_one_guild_failing_does_not_stop_the_next():
    bot = SimpleNamespace(user=SimpleNamespace(id=999),
                          guilds=[SimpleNamespace(id=1), SimpleNamespace(id=2)])
    publish = AsyncMock(side_effect=[RuntimeError("spaces down"), "https://x"])
    with patch.object(publisher, "get_event", return_value=EVENT), \
         patch.object(publisher, "publish_event", publish):
        await publisher.publish_active(bot)

    assert publish.await_count == 2


@pytest.mark.asyncio
async def test_inactive_events_are_not_published():
    bot = SimpleNamespace(user=SimpleNamespace(id=999), guilds=[SimpleNamespace(id=1)])
    publish = AsyncMock()
    with patch.object(publisher, "get_event", return_value={**EVENT, "active": False}), \
         patch.object(publisher, "publish_event", publish):
        await publisher.publish_active(bot)

    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_upload_reports_no_url():
    helper = MagicMock()
    helper.upload_public_html = AsyncMock(return_value=SimpleNamespace(success=False, object_path=None))
    with patch.object(publisher, "build_payload", AsyncMock(return_value={"rows": []})), \
         patch.object(publisher, "DigitalOceanHelper", return_value=helper):
        assert await publisher.publish_event("1", EVENT, "999") is None
