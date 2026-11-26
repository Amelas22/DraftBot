"""Per-guild route for new drafts: config helpers, queue decision, /draft_route."""
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import config
from cogs.draft_control import DraftControlCog
from database.models_base import Base
from models.draft_session import DraftSession
from models.session_details import SessionDetails
from sessions.random_session import RandomSession

G = "111"
OTHER = "222"


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    """Fresh in-memory guild configs; nothing is written under configs/."""
    monkeypatch.setattr(config.bot_config, "configs", {G: {"features": {}}, OTHER: {"features": {}}})
    monkeypatch.setattr(config.bot_config, "save_config", lambda guild_id: None)


@pytest.fixture
def hosting_on(monkeypatch):
    monkeypatch.setenv("DRAFTMANCER_API_URL", "http://hosted.invalid")
    monkeypatch.setenv("DRAFTMANCER_API_TOKEN", "t")


def test_route_defaults_to_hosted_when_configured(hosting_on):
    assert config.hosted_route_for(G) is True


def test_draftmancer_setting_turns_hosted_off_for_that_guild_only(hosting_on):
    config.update_setting(G, config.DRAFTMANCER_ROUTE_SETTING, "draftmancer")
    assert config.hosted_route_for(G) is False
    assert config.hosted_route_for(OTHER) is True


def test_no_env_pair_means_draftmancer_whatever_the_guild_says():
    config.update_setting(G, config.DRAFTMANCER_ROUTE_SETTING, "hosted")
    assert config.hosted_route_for(G) is False


def test_unknown_setting_reads_as_hosted(hosting_on):
    config.update_setting(G, config.DRAFTMANCER_ROUTE_SETTING, "bogus")
    assert config.draftmancer_route_setting(G) == "hosted"


@pytest_asyncio.fixture
async def test_db():
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    temp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{temp.name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()
    os.unlink(temp.name)


@pytest.mark.asyncio
async def test_new_queue_follows_the_guild_route(test_db, hosting_on):
    config.update_setting(G, config.DRAFTMANCER_ROUTE_SETTING, "draftmancer")
    interaction = MagicMock(spec_set=discord.Interaction)
    interaction.user = MagicMock(id=123456, display_name="U")
    interaction.guild_id = int(G)
    interaction.guild = MagicMock(id=int(G))
    interaction.response = AsyncMock()
    message = MagicMock(id="987654321")
    message.channel = MagicMock(id="111222333")
    interaction.original_response = AsyncMock(return_value=message)
    interaction.client = MagicMock()
    details = SessionDetails(interaction)
    details.cube_choice = "test-cube"
    manager = MagicMock()
    manager.keep_connection_alive = AsyncMock()
    manager.socket_client = MagicMock(connected=False)

    with patch("sessions.base_session.DraftSetupManager", return_value=manager) as cls, \
         patch("sessions.base_session.PersistentView"), \
         patch("sessions.base_session.make_message_sticky", new_callable=AsyncMock), \
         patch("sessions.base_session.get_session_deletion_hours", return_value=5), \
         patch("sessions.base_session.get_cube_thumbnail_url", return_value="https://x/t.jpg"):
        session = RandomSession(details, session_factory=test_db)
        await session.create_draft_session(interaction, interaction.client)

    cls.assert_called_once()
    async with test_db() as s:
        row = await s.scalar(select(DraftSession).filter_by(session_id=details.session_id))
    assert row is not None and row.draftmancer_route is None


def _ctx():
    ctx = MagicMock()
    ctx.guild = MagicMock(id=int(G))
    ctx.followup.send = AsyncMock()
    return ctx


def _reply(ctx) -> str:
    return ctx.followup.send.call_args.args[0]


@pytest.mark.asyncio
async def test_draft_route_command_is_admin_only(hosting_on):
    ctx = _ctx()
    with patch("cogs.draft_control.is_bot_manager", AsyncMock(return_value=False)):
        await DraftControlCog(MagicMock())._do_draft_route(ctx, "draftmancer")
    assert config.draftmancer_route_setting(G) == "hosted"
    assert "Only bot managers" in _reply(ctx)


@pytest.mark.asyncio
async def test_draft_route_command_sets_and_reports(hosting_on, monkeypatch):
    cog = DraftControlCog(MagicMock())
    with patch("cogs.draft_control.is_bot_manager", AsyncMock(return_value=True)):
        ctx = _ctx()
        await cog._do_draft_route(ctx, "draftmancer")
        assert config.draftmancer_route_setting(G) == "draftmancer"
        assert "draftmancer" in _reply(ctx) and "keep their route" in _reply(ctx)

        ctx = _ctx()
        await cog._do_draft_route(ctx, None)
        assert "draftmancer" in _reply(ctx) and "keep their route" not in _reply(ctx)

        monkeypatch.delenv("DRAFTMANCER_API_URL")
        ctx = _ctx()
        await cog._do_draft_route(ctx, None)
        assert "no effect until hosting is configured" in _reply(ctx)
