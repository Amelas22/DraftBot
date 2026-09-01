"""/regenerate_rooms — the admin repair for discord-api-docs#6573.

The command is glue: it resolves the draft, hands off to regenerate_team_rooms,
and reports. What is worth pinning down is that it REFUSES clearly rather than
half-running, because an admin reaching for this is already dealing with a room
nobody can see and a silent no-op looks identical to the bug being repaired.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import sent_to_invoker


def _session():
    return SimpleNamespace(session_id="s-1", friendly_id="reckless-crew-92")


def make_ctx(guild_id=1355718878298116096):
    ctx = MagicMock()
    ctx.guild.id = guild_id
    ctx.author.name = "aberdasher"
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    return ctx


def _cog():
    from cogs.admin_commands import AdminCommands
    return AdminCommands(MagicMock())


@pytest.mark.asyncio
async def test_an_unknown_draft_is_refused_without_touching_any_channel():
    ctx = make_ctx()
    cog = _cog()
    regenerate = AsyncMock()

    with patch("models.draft_session.DraftSession.get_by_friendly_id",
               AsyncMock(return_value=None)), \
         patch("helpers.room_regeneration.regenerate_team_rooms", regenerate):
        await cog.regenerate_rooms.callback(cog, ctx, "no-such-draft", "Red-Team")

    regenerate.assert_not_awaited()
    assert "no-such-draft" in sent_to_invoker(ctx)


@pytest.mark.asyncio
async def test_a_successful_rebuild_reports_the_new_room():
    from helpers.room_regeneration import RegenerationResult

    ctx = make_ctx()
    cog = _cog()
    session = _session()

    with patch("models.draft_session.DraftSession.get_by_friendly_id",
               AsyncMock(return_value=session)), \
         patch("helpers.room_regeneration.regenerate_team_rooms",
               AsyncMock(return_value=(RegenerationResult([2, 3], 99, True), None))):
        await cog.regenerate_rooms.callback(cog, ctx, "reckless-crew-92", "Red-Team")

    assert "<#99>" in sent_to_invoker(ctx)


@pytest.mark.asyncio
async def test_a_draft_whose_rooms_are_already_gone_is_reported_not_claimed():
    """regenerate_team_rooms returns None when it changed nothing. Reporting that
    as success would send an admin away believing a broken room was repaired."""
    ctx = make_ctx()
    cog = _cog()
    session = _session()

    with patch("models.draft_session.DraftSession.get_by_friendly_id",
               AsyncMock(return_value=session)), \
         patch("helpers.room_regeneration.regenerate_team_rooms",
               AsyncMock(return_value=(None, "Red-Team has no rooms recorded"))):
        await cog.regenerate_rooms.callback(cog, ctx, "reckless-crew-92", "Red-Team")

    assert "no rooms recorded" in sent_to_invoker(ctx)


@pytest.mark.asyncio
async def test_a_rebuild_whose_pools_did_not_come_back_says_so():
    """The rooms are rebuilt either way, so this is a warning beside a success
    rather than a failure -- but it must not be silent: the team is missing the
    card lists they play the matches from."""
    from helpers.room_regeneration import RegenerationResult

    ctx = make_ctx()
    cog = _cog()

    with patch("models.draft_session.DraftSession.get_by_friendly_id",
               AsyncMock(return_value=_session())), \
         patch("helpers.room_regeneration.regenerate_team_rooms",
               AsyncMock(return_value=(RegenerationResult([2, 3], 99, False), None))):
        await cog.regenerate_rooms.callback(cog, ctx, "reckless-crew-92", "Red-Team")

    reply = sent_to_invoker(ctx)
    assert "<#99>" in reply and "NOT reposted" in reply
