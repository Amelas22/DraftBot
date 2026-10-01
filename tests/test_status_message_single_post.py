"""A draft's status message is posted once, however many callers race to post it.

send_session_status_message checked status_message_id, awaited channel.send,
and only then stored the new id. Team creation nudges the manager to post its
status at the same moment a drafter already in Draftmancer triggers a users
update -- in production both callers found no message, both posted, and the
channel kept a second status message frozen at the first drafter while every
later update edited the other.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from conftest import make_manager
from services.draft_setup_manager import DraftSession

pytestmark = pytest.mark.usefixtures("clean_manager_registry")


def _channel():
    """A channel whose send yields, the way a real Discord round trip does."""
    posted = []

    async def send(content=None, embed=None, view=None):
        await asyncio.sleep(0.01)
        message = SimpleNamespace(id=1000 + len(posted), edit=AsyncMock())
        posted.append(message)
        return message

    async def fetch_message(message_id):
        found = next((m for m in posted if m.id == message_id), None)
        if found is None:
            raise discord.NotFound(MagicMock(status=404), "Unknown Message")
        return found

    channel = MagicMock()
    channel.name = "draft-room"
    channel.send = AsyncMock(side_effect=send)
    channel.fetch_message = AsyncMock(side_effect=fetch_message)
    return channel, posted


@pytest.mark.asyncio
async def test_two_callers_at_once_post_one_status_message():
    manager = make_manager(session_id="s1")
    manager.update_draft_session_field = AsyncMock()
    channel, posted = _channel()
    row = SimpleNamespace(sign_ups={"u1": "Ann", "u2": "Ben"})

    with patch.object(DraftSession, "get_by_session_id", AsyncMock(return_value=row)):
        await asyncio.gather(manager.send_session_status_message(channel),
                             manager.send_session_status_message(channel))

    assert len(posted) == 1, f"{len(posted)} status messages were posted"
    assert posted[0].edit.await_count == 1, "the second caller should edit the first's message"
    assert manager.status_message_id == str(posted[0].id)


@pytest.mark.asyncio
async def test_a_replaced_status_message_is_tracked_from_then_on():
    """The seating updates used to post a replacement but keep the dead id."""
    manager = make_manager(session_id="s1")
    manager.update_draft_session_field = AsyncMock()
    channel, posted = _channel()
    manager.status_message_id = "999"           # deleted from the channel

    await manager._post_status(channel, "✅ Seating order set")

    assert len(posted) == 1
    assert manager.status_message_id == str(posted[0].id)
    manager.update_draft_session_field.assert_awaited_once_with(
        "status_message_id", str(posted[0].id))


@pytest.mark.asyncio
@pytest.mark.parametrize("seated, says", [
    (True, "Seating order set successfully"),
    (False, "Failed to set seating order"),
])
async def test_seating_is_reported_even_if_the_first_status_post_failed(seated, says):
    """If the first status post failed there is no tracked message, and the
    seating outcome used to be skipped outright -- and nothing posts a status
    again once seating is set. _post_status posts a message when there is none."""
    manager = make_manager(session_id="s1")
    manager.update_draft_session_field = AsyncMock()
    manager.initiate_ready_check = AsyncMock()
    manager.status_message_id = None
    channel, posted = _channel()
    manager._get_draft_channel = AsyncMock(return_value=channel)
    manager.set_seating_order = AsyncMock(return_value=(seated, [] if seated else ["Ann"]))

    with patch("services.draft_setup_manager.asyncio.sleep", AsyncMock()):
        await manager.attempt_seating_order(["Ann", "Ben"])

    contents = [c.kwargs.get("content") or "" for c in channel.send.await_args_list]
    assert any(says in c for c in contents), contents
