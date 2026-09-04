"""Publishing the page, and surviving every way that can fail.

The embed is the deliverable; the page is an enhancement to it. So this module
returns None on any failure rather than raising -- a draft that loses its page
still gets its log posted.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.draft_session import DraftSession
from services import draft_table_publisher


async def seed_session(session_id, friendly_id, **overrides):
    fields = dict(session_id=session_id, friendly_id=friendly_id, guild_id="g1",
                  cube="PowerLSV", session_type="premade", sign_ups={},
                  team_a=[], team_b=[])
    fields.update(overrides)
    async with db_session() as session:
        session.add(DraftSession(**fields))
        await session.commit()
    async with db_session() as session:
        from sqlalchemy import select
        return (await session.execute(
            select(DraftSession).filter_by(session_id=session_id))).scalar_one()


def patch_render_and_upload(monkeypatch, *, html="<html></html>", success=True):
    async def fake_render(draft_data, meta):
        return html
    monkeypatch.setattr(draft_table_publisher.draft_table_page, "render", fake_render)

    helper = MagicMock()
    helper.upload_public_html = AsyncMock(
        return_value=MagicMock(success=success, object_path="drafttable/x.html"))
    helper.get_public_url = MagicMock(
        return_value="https://magic-draft-logs.nyc3.digitaloceanspaces.com/drafttable/x.html")
    monkeypatch.setattr(draft_table_publisher, "DigitalOceanHelper",
                        MagicMock(return_value=helper))
    return helper


@pytest.mark.asyncio
async def test_publish_returns_the_public_url(test_db, monkeypatch):  # noqa: F811
    ds = await seed_session("s1", "ashas-favor-55")
    patch_render_and_upload(monkeypatch)

    url = await draft_table_publisher.publish({"users": {}}, ds)

    assert url.endswith("/drafttable/x.html")


@pytest.mark.asyncio
async def test_filename_is_the_friendly_id(test_db, monkeypatch):  # noqa: F811
    ds = await seed_session("s1", "ashas-favor-55")
    helper = patch_render_and_upload(monkeypatch)

    await draft_table_publisher.publish({"users": {}}, ds)

    assert helper.upload_public_html.await_args.args[2] == "ashas-favor-55.html"


@pytest.mark.asyncio
async def test_a_colliding_friendly_id_gets_a_suffix(test_db, monkeypatch):  # noqa: F811
    """friendly_id is not unique, and the filename becomes a permanent URL."""
    await seed_session(
        "older", "songstitcher-23",
        drafttable_url="https://x/drafttable/songstitcher-23.html")
    ds = await seed_session("newer-abcdef", "songstitcher-23")
    helper = patch_render_and_upload(monkeypatch)

    await draft_table_publisher.publish({"users": {}}, ds)

    assert helper.upload_public_html.await_args.args[2] == "songstitcher-23-abcdef.html"


@pytest.mark.asyncio
async def test_its_own_published_url_is_not_a_collision(test_db, monkeypatch):  # noqa: F811
    """Republishing the same draft must reuse its own filename, not suffix it."""
    ds = await seed_session(
        "s1", "ashas-favor-55",
        drafttable_url="https://x/drafttable/ashas-favor-55.html")
    helper = patch_render_and_upload(monkeypatch)

    await draft_table_publisher.publish({"users": {}}, ds)

    assert helper.upload_public_html.await_args.args[2] == "ashas-favor-55.html"


@pytest.mark.asyncio
async def test_returns_none_when_the_upload_fails(test_db, monkeypatch):  # noqa: F811
    ds = await seed_session("s1", "ashas-favor-55")
    patch_render_and_upload(monkeypatch, success=False)

    assert await draft_table_publisher.publish({"users": {}}, ds) is None


@pytest.mark.asyncio
async def test_returns_none_when_rendering_raises(test_db, monkeypatch):  # noqa: F811
    """An odd log shape must not cost the draft its embed."""
    ds = await seed_session("s1", "ashas-favor-55")
    patch_render_and_upload(monkeypatch)

    async def boom(draft_data, meta):
        raise ValueError("unexpected log shape")
    monkeypatch.setattr(draft_table_publisher.draft_table_page, "render", boom)

    assert await draft_table_publisher.publish({"users": {}}, ds) is None
