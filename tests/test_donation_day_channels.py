"""Receipts are posted once each; the leaderboard is one message, edited in place."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import services.donation_day_discord as dd_discord
from services.donation_day import Credit, Snapshot

T0 = datetime(2026, 10, 16, 19, 0)


def _state(**kw):
    base = {"receipts_channel_id": "10", "leaderboard_channel_id": "20",
            "leaderboard_message_id": None, "announced": []}
    return {**base, **kw}


def _bot(channel):
    bot = MagicMock()
    bot.get_channel.return_value = channel
    return bot


def _channel():
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(return_value=SimpleNamespace(id=555))
    channel.fetch_message = AsyncMock()
    return channel


def _snapshot(n):
    credits = [Credit(str(i), 10, T0, "paid", key=f"settle:{i}", method="wallet")
               for i in range(n)]
    return Snapshot(credits=credits, outstanding=0, names={}, anonymous=set())


@pytest.mark.asyncio
async def test_each_receipt_is_posted_once_and_remembered():
    channel = _channel()
    with patch.object(dd_discord, "save_state") as save:
        state = await dd_discord.post_receipts(_bot(channel), "g", _snapshot(2), _state())
        again = await dd_discord.post_receipts(_bot(channel), "g", _snapshot(2), state)

    assert channel.send.await_count == 2
    assert sorted(again["announced"]) == ["settle:0", "settle:1"]
    assert save.call_count == 2


@pytest.mark.asyncio
async def test_receipts_never_ping_anyone():
    channel = _channel()
    with patch.object(dd_discord, "save_state"):
        await dd_discord.post_receipts(_bot(channel), "g", _snapshot(1), _state())

    mentions = channel.send.await_args.kwargs["allowed_mentions"]
    assert not mentions.everyone and not mentions.users and not mentions.roles


@pytest.mark.asyncio
async def test_receipts_are_capped_per_tick():
    channel = _channel()
    with patch.object(dd_discord, "save_state"):
        state = await dd_discord.post_receipts(_bot(channel), "g", _snapshot(25), _state())

    assert channel.send.await_count == dd_discord.MAX_RECEIPTS_PER_TICK
    assert len(state["announced"]) == dd_discord.MAX_RECEIPTS_PER_TICK


@pytest.mark.asyncio
async def test_a_failed_send_keeps_what_was_already_announced():
    channel = _channel()
    channel.send = AsyncMock(side_effect=[SimpleNamespace(id=1), RuntimeError("discord down")])
    saved = []
    with patch.object(dd_discord, "save_state", side_effect=lambda g, s: saved.append(dict(s))):
        with pytest.raises(RuntimeError):
            await dd_discord.post_receipts(_bot(channel), "g", _snapshot(2), _state())

    assert saved[-1]["announced"] == ["settle:0"]


@pytest.mark.asyncio
async def test_no_receipts_channel_posts_nothing():
    bot = _bot(_channel())
    with patch.object(dd_discord, "save_state"):
        state = await dd_discord.post_receipts(bot, "g", _snapshot(2),
                                               _state(receipts_channel_id=None))

    bot.get_channel.assert_not_called()
    assert state["announced"] == []


@pytest.mark.asyncio
async def test_the_leaderboard_is_sent_once_then_edited():
    channel = _channel()
    message = MagicMock()
    message.edit = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=message)
    payload = {"name": "DD", "goal": 100, "raised": 10, "pledged": 10,
               "updated_at": "2026-10-16T19:03", "rows": []}
    with patch.object(dd_discord, "save_state"):
        state = await dd_discord.update_leaderboard(_bot(channel), "g", payload, None, _state())
        await dd_discord.update_leaderboard(_bot(channel), "g", payload, None, state)

    assert state["leaderboard_message_id"] == "555"
    channel.send.assert_awaited_once()
    message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_deleted_leaderboard_message_is_replaced():
    channel = _channel()
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    payload = {"name": "DD", "goal": 100, "raised": 0, "pledged": 0,
               "updated_at": "", "rows": []}
    with patch.object(dd_discord, "save_state"):
        state = await dd_discord.update_leaderboard(
            _bot(channel), "g", payload, None, _state(leaderboard_message_id="999"))

    channel.send.assert_awaited_once()
    assert state["leaderboard_message_id"] == "555"


@pytest.mark.asyncio
async def test_a_discord_failure_does_not_stop_the_page():
    import services.donation_day_publisher as publisher
    helper = MagicMock()
    helper.upload_public_html = AsyncMock(
        return_value=SimpleNamespace(success=True, object_path="donations/dd/index.html"))
    helper.get_public_url.return_value = "https://x/donations/dd/index.html"
    event = {"name": "DD", "goal_usd": 100, "starts_at": "2026-10-16T12:00",
             "page_key": "dd", "active": True}
    with patch.object(publisher, "gather_snapshot", AsyncMock(return_value=_snapshot(1))), \
         patch.object(publisher, "DigitalOceanHelper", return_value=helper), \
         patch.object(publisher, "get_state", return_value=_state()), \
         patch.object(publisher, "post_receipts", AsyncMock(side_effect=RuntimeError("down"))), \
         patch.object(publisher, "update_leaderboard", AsyncMock(side_effect=RuntimeError("down"))):
        url = await publisher.publish_event("1", event, "999", bot=MagicMock())

    assert url == "https://x/donations/dd/index.html"
    helper.upload_public_html.assert_awaited_once()


PREVIOUS = {"receipts_channel_id": "10", "leaderboard_channel_id": "20",
            "leaderboard_message_id": "77", "announced": ["settle:1"]}


def test_setup_on_a_running_event_keeps_announced_and_the_leaderboard_message():
    assert dd_discord.state_for_setup(PREVIOUS, True, "10", "20") == PREVIOUS


def test_setup_on_a_running_event_resets_only_the_channel_that_changed():
    moved_receipts = dd_discord.state_for_setup(PREVIOUS, True, "11", "20")
    moved_board = dd_discord.state_for_setup(PREVIOUS, True, "10", "21")

    assert moved_receipts["announced"] == [] and moved_receipts["leaderboard_message_id"] == "77"
    assert moved_board["announced"] == ["settle:1"] and moved_board["leaderboard_message_id"] is None
    assert moved_board["leaderboard_channel_id"] == "21"


def test_setup_for_a_new_event_starts_clean():
    for previous, active in ((PREVIOUS, False), (None, True)):
        state = dd_discord.state_for_setup(previous, active, "10", None)
        assert state == {"receipts_channel_id": "10", "leaderboard_channel_id": None,
                         "leaderboard_message_id": None, "announced": []}
