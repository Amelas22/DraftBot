import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from database.db_session import db_session
from models.scheduler import SchedulerLink
from services.scheduler_links import LINK_DAYS, LinkIdentity, issue_link, resolve_token

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)


async def _rows() -> list[SchedulerLink]:
    async with db_session() as session:
        return list((await session.execute(select(SchedulerLink))).scalars())


@pytest.mark.asyncio
async def test_issue_link_uses_base_url(test_db, monkeypatch):
    monkeypatch.setenv("SCHEDULER_BASE_URL", "https://dev.draftbot.fyi")
    url = await issue_link("123", "456", "usual", NOW)
    assert re.fullmatch(r"https://dev\.draftbot\.fyi/#[A-Za-z0-9_-]{22}", url)
    (row,) = await _rows()
    assert url.endswith("#" + row.token)
    assert (row.user_id, row.guild_id, row.view) == ("123", "456", "usual")
    assert row.issued_utc == datetime(2026, 10, 22, 12, 0, 0)
    assert row.expires_utc == datetime(2026, 10, 29, 12, 0, 0)
    assert row.removed_utc is None


@pytest.mark.asyncio
async def test_trailing_slash_on_base_url_is_dropped(test_db, monkeypatch):
    monkeypatch.setenv("SCHEDULER_BASE_URL", "https://dev.draftbot.fyi/")
    assert (await issue_link("1", "2", "usual", NOW)).startswith("https://dev.draftbot.fyi/#")


@pytest.mark.asyncio
async def test_link_fits_a_discord_button(test_db):
    assert len(await issue_link("123", "456", "usual", NOW)) <= 512


@pytest.mark.asyncio
async def test_tokens_differ_per_press(test_db):
    a = await issue_link("123", "456", "usual", NOW)
    b = await issue_link("123", "456", "usual", NOW)
    assert a != b
    assert len(await _rows()) == 2


@pytest.mark.asyncio
async def test_resolve_token_valid_expired_removed_unknown(test_db):
    token = (await issue_link("123", "456", "usual", NOW)).split("#", 1)[1]
    expires = NOW + timedelta(days=LINK_DAYS)
    assert await resolve_token(token, NOW) == LinkIdentity("123", "456", "usual", expires)
    assert await resolve_token(token, expires) is not None
    assert await resolve_token(token, expires + timedelta(seconds=1)) is None
    assert await resolve_token("x" * 22, NOW) is None

    async with db_session() as session:
        row = (await session.execute(select(SchedulerLink))).scalar_one()
        row.removed_utc = datetime(2026, 10, 23)
    assert await resolve_token(token, NOW) is None
