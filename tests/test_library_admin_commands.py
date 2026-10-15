from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs import library_commands as lc


def _ctx():
    ctx = MagicMock()
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    return ctx


async def _run(monkeypatch, result):
    monkeypatch.setattr("helpers.cube_list.refresh_now", AsyncMock(return_value=result))
    cog = lc.LibraryCommands(MagicMock())
    ctx = _ctx()
    await lc.LibraryCommands.refresh_cube.callback(cog, ctx, "PowerLSV")
    return ctx.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_a_changed_cube_says_so(monkeypatch):
    said = await _run(monkeypatch, ([{"name": "Bolt", "qty": 2}], True))
    assert "2 cards" in said and "changed" in said


@pytest.mark.asyncio
async def test_an_unchanged_cube_says_so(monkeypatch):
    said = await _run(monkeypatch, ([{"name": "Bolt", "qty": 1}], False))
    assert "no change" in said


@pytest.mark.asyncio
async def test_an_unreadable_cube_changes_nothing(monkeypatch):
    said = await _run(monkeypatch, (None, False))
    assert "Couldn't read" in said


def test_it_is_limited_to_bot_managers():
    checks = getattr(lc.LibraryCommands.refresh_cube, "checks", [])
    assert checks, "refresh-cube must carry the bot-manager check"
