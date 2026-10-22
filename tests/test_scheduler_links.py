import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from database.db_session import db_session
from models.scheduler import SchedulerLink
from scheduler_fakes import FakeSchedulerSpaces
from services.scheduler_links import LINK_DAYS, issue_usual_link, link_issued_since
from services.scheduler_spaces import PAGE_URL, SpacesError

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)


async def _rows() -> list[SchedulerLink]:
    async with db_session() as session:
        return list((await session.execute(select(SchedulerLink))).scalars())


@pytest.mark.asyncio
async def test_issue_writes_file8_and_row(test_db):
    spaces = FakeSchedulerSpaces()
    url = await issue_usual_link(spaces, "123", NOW)
    assert url.startswith(PAGE_URL + "#")
    token = url.split("#", 1)[1]
    assert len(token) == 22
    key = f"scheduler/link/{token}.json"
    assert key in spaces.public
    body = json.loads(spaces.objects[key][0])
    assert body == {
        "format": "draftbot.scheduler.link",
        "version": 1,
        "view": "usual",
        "user": "123",
        "exp": (NOW + timedelta(days=LINK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "urls": {
            "usual_get": "https://fake/get_object/scheduler/availability/123.json",
            "usual_put": "https://fake/put_object/scheduler/availability/123.json",
        },
    }
    rows = await _rows()
    assert len(rows) == 1
    assert rows[0].token == token and rows[0].user_id == "123" and rows[0].view == "usual"
    assert rows[0].issued_utc == datetime(2026, 10, 22, 12, 0, 0)
    assert rows[0].expires_utc == datetime(2026, 10, 29, 12, 0, 0)
    assert rows[0].removed_utc is None


@pytest.mark.asyncio
async def test_link_fits_a_discord_button(test_db):
    assert len(await issue_usual_link(FakeSchedulerSpaces(), "123", NOW)) <= 512


@pytest.mark.asyncio
async def test_tokens_differ_per_press(test_db):
    spaces = FakeSchedulerSpaces()
    a = await issue_usual_link(spaces, "123", NOW)
    b = await issue_usual_link(spaces, "123", NOW)
    assert a != b
    assert len(await _rows()) == 2


@pytest.mark.asyncio
async def test_spaces_failure_commits_nothing(test_db):
    class Broken(FakeSchedulerSpaces):
        async def put_public_json(self, key, body):
            raise SpacesError("down")

    with pytest.raises(SpacesError):
        await issue_usual_link(Broken(), "123", NOW)
    assert await _rows() == []


@pytest.mark.asyncio
async def test_commit_failure_warns_with_the_orphaned_key_and_reraises(test_db, monkeypatch):
    from loguru import logger

    import services.scheduler_links as links

    class Boom:
        async def __aenter__(self):
            raise RuntimeError("commit failed")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(links, "db_session", lambda: Boom())
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    spaces = FakeSchedulerSpaces()
    try:
        with pytest.raises(RuntimeError, match="commit failed"):
            await issue_usual_link(spaces, "123", NOW)
    finally:
        logger.remove(sink)
    (key,) = [k for k in spaces.public if k.startswith("scheduler/link/")]
    assert any(key in m for m in messages)


@pytest.mark.asyncio
async def test_link_issued_since(test_db):
    cutoff = NOW - timedelta(days=7)
    assert await link_issued_since(cutoff) is False
    await issue_usual_link(FakeSchedulerSpaces(), "123", NOW)
    assert await link_issued_since(cutoff) is True
    assert await link_issued_since(NOW + timedelta(seconds=1)) is False
