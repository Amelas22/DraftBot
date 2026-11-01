"""A hosted queue that goes away before it starts closes its session on the server.

A cancelled, reaped or abandoned draft has no row left to poll, so nothing else
would ever stop the session it holds open on the hosted server. Closing it is
best effort: a session already gone (404) or already ended or stopped (409) is
what was wanted, and a server that cannot be reached must not block the user's
action -- the server reaps a never-started session on its own after a while.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import hosted_draft
from services.hosted_draftmancer import HostedRefused, HostedUnavailable
from session import get_draft_session

from conftest import run_until_sleep
from hosted_fakes import (  # noqa: F401 -- fixtures are used by name
    DRAFT_ID, SID, FakeChannel, _row, _seed, bot, channel, hosted,
)


# --- the helper -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_stopping_a_hosted_draft_calls_stop_and_stops_polling_it(hosted):
    await hosted_draft.stop_if_hosted(SID, DRAFT_ID, "hosted")
    assert hosted.calls == [("stop", DRAFT_ID)]
    assert SID in hosted_draft._DONE


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [None, "fallback"])
async def test_a_draft_not_on_the_hosted_server_is_left_alone(hosted, route):
    await hosted_draft.stop_if_hosted(SID, DRAFT_ID, route)
    assert hosted.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [HostedRefused(404, {}), HostedRefused(409, {}),
                                   HostedUnavailable("down"), HostedRefused(401, {}),
                                   RuntimeError("anything")])
async def test_closing_never_raises(hosted, error):
    hosted.fail("stop", error)
    await hosted_draft.stop_if_hosted(SID, DRAFT_ID, "hosted")
    assert hosted.calls == [("stop", "raised")]


# --- the cancel button ------------------------------------------------------------

def _cancel(bot):
    import views
    view = views.CancelConfirmationView(bot, SID, "Ann")
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return views, view, interaction


@pytest.mark.asyncio
async def test_cancelling_a_hosted_queue_closes_its_session(hosted, bot):
    await _seed(stage=None)
    views, view, interaction = _cancel(bot)
    with patch.object(views.DraftSetupManager, "cancel_for_session", AsyncMock()):
        await view.confirm_button.callback(interaction)

    assert ("stop", DRAFT_ID) in hosted.calls
    assert await get_draft_session(SID) is None


@pytest.mark.asyncio
async def test_a_cancel_goes_through_when_the_hosted_server_is_down(hosted, bot):
    await _seed(stage=None)
    hosted.fail("stop", HostedUnavailable("down"))
    views, view, interaction = _cancel(bot)
    with patch.object(views.DraftSetupManager, "cancel_for_session", AsyncMock()):
        await view.confirm_button.callback(interaction)

    assert await get_draft_session(SID) is None


@pytest.mark.asyncio
async def test_cancelling_a_manager_queue_asks_no_hosted_server(hosted, bot):
    await _seed(stage=None, route=None)
    views, view, interaction = _cancel(bot)
    with patch.object(views.DraftSetupManager, "cancel_for_session", AsyncMock()):
        await view.confirm_button.callback(interaction)

    assert hosted.calls == []


# --- the inactivity reaper ------------------------------------------------------

async def _one_cleanup_pass():
    from utils import cleanup_sessions_task

    reaper = MagicMock()
    reaper.get_channel.return_value = None
    with patch("utils.release_draft_pool", AsyncMock()):
        await run_until_sleep(cleanup_sessions_task(reaper), 600)


@pytest.mark.asyncio
async def test_an_expired_hosted_queue_closes_its_session(hosted):
    await _seed(stage=None, deletion_time=datetime.now() - timedelta(minutes=1))
    await _one_cleanup_pass()
    assert hosted.calls == [("stop", DRAFT_ID)]


@pytest.mark.asyncio
async def test_an_open_hosted_queue_keeps_its_session(hosted):
    await _seed(stage=None, deletion_time=datetime.now() + timedelta(minutes=30))
    await _one_cleanup_pass()
    assert hosted.calls == []


# --- /abandon -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_abandoning_a_hosted_draft_closes_its_session(hosted):
    from cogs.draft_control import abandon_draft_session

    await _seed(stage="teams")
    with patch("cogs.draft_control.release_draft_pool", AsyncMock()):
        assert await abandon_draft_session(SID) is True

    assert hosted.calls == [("stop", DRAFT_ID)]
    assert (await _row()).session_stage == "abandoned"


@pytest.mark.asyncio
async def test_an_abandon_goes_through_when_the_hosted_server_is_down(hosted):
    from cogs.draft_control import abandon_draft_session

    await _seed(stage="teams")
    hosted.fail("stop", HostedUnavailable("down"))
    with patch("cogs.draft_control.release_draft_pool", AsyncMock()):
        assert await abandon_draft_session(SID) is True

    assert (await _row()).session_stage == "abandoned"


@pytest.mark.asyncio
async def test_abandoning_a_manager_draft_asks_no_hosted_server(hosted):
    from cogs.draft_control import abandon_draft_session

    await _seed(stage="teams", route=None)
    with patch("cogs.draft_control.release_draft_pool", AsyncMock()):
        assert await abandon_draft_session(SID) is True

    assert hosted.calls == []
