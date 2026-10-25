"""An MTGO handle held by another account: a player can't take it, an admin can move it."""
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from cogs.mtgo_link_cog import MtgoLinkCommands
from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import db_session
from models.mtgo_account import MtgoAccount

OLD, NEW, ADMIN = 100, 200, 300


async def _links():
    async with db_session() as session:
        rows = (await session.execute(select(MtgoAccount))).scalars().all()
        return {r.discord_user_id: r.mtgo_username for r in rows}


def _ctx(author_id):
    ctx = MagicMock()
    ctx.author.id = author_id
    ctx.guild.id = 1
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    return ctx


def _member(user_id):
    m = MagicMock()
    m.id = user_id
    return m


@pytest.mark.asyncio
async def test_link_refuses_a_held_handle_without_reassign(test_db):
    await MtgoAccount.link(OLD, "GrapeshotGooley")

    assert await MtgoAccount.link(NEW, "grapeshotgooley") == ("conflict", str(OLD))
    assert await _links() == {str(OLD): "GrapeshotGooley"}


@pytest.mark.asyncio
async def test_reassign_moves_the_handle_onto_an_existing_link(test_db):
    await MtgoAccount.link(OLD, "GrapeshotGooley")
    await MtgoAccount.link(NEW, "Grapeshot2")

    result = await MtgoAccount.link(NEW, " grapeshotgooley ", reassign=True)

    assert result == ("reassigned", str(OLD))
    assert await _links() == {str(NEW): "grapeshotgooley"}


@pytest.mark.asyncio
async def test_reassign_to_the_current_holder_is_a_plain_update(test_db):
    await MtgoAccount.link(NEW, "GrapeshotGooley")

    assert await MtgoAccount.link(NEW, "GrapeshotGooley", reassign=True) == (
        "ok", "GrapeshotGooley")


@pytest.mark.asyncio
async def test_self_service_link_still_points_at_the_admin_command(test_db):
    await MtgoAccount.link(OLD, "GrapeshotGooley")
    ctx = _ctx(NEW)

    await MtgoLinkCommands.link_mtgo.callback(MtgoLinkCommands(MagicMock()), ctx, "GrapeshotGooley")

    assert "/link_mtgo_for" in ctx.followup.send.call_args.args[0]
    assert await _links() == {str(OLD): "GrapeshotGooley"}


@pytest.mark.asyncio
async def test_admin_link_moves_the_handle_and_names_who_lost_it(test_db):
    await MtgoAccount.link(OLD, "GrapeshotGooley")
    ctx = _ctx(ADMIN)

    await MtgoLinkCommands.link_mtgo_for.callback(
        MtgoLinkCommands(MagicMock()), ctx, _member(NEW), "GrapeshotGooley")

    reply = ctx.followup.send.call_args.args[0]
    assert f"<@{NEW}>" in reply and f"<@{OLD}>" in reply and "**GrapeshotGooley**" in reply
    assert await _links() == {str(NEW): "GrapeshotGooley"}
