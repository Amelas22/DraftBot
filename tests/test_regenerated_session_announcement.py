"""When the bot moves a draft to a new Draftmancer room, it has to say so.

regenerate_draft_session swaps draft_id and draft_link, and every link the bot
renders afterwards is correct. The links already in people's hands are not: the
sign-up confirmation is an ephemeral message sent at sign-up time, so it cannot be
edited later, and nothing about it suggests the room has changed.

frontier-guide-69 (2026-09-02) regenerated five seconds after the queue opened.
Twelve minutes later the bot's own session held zero non-bot users while six
players sat in the abandoned room; the draft was given up on and re-run by hand in
a session a player made themselves.

Editing the stale copies is impossible, so the fix is an announcement carrying a
fresh personalised link for everyone signed up.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import fake_db_session, make_manager


def _swap_succeeds():
    """The UPDATE reports one row changed -- the room swap happened."""
    return fake_db_session(rowcount=1)


def _draft_session():
    """Two signed-up players, so the announcement has someone to address."""
    ds = SimpleNamespace(sign_ups={"11": "Alice", "22": "Bob"},
                         draft_channel_id="1543769252744528028",
                         draft_link="https://draftmancer.com/?session=DBNEWID11")
    ds.get_draft_link_for_user = lambda name: f"{ds.draft_link}&userName={name}"
    return ds


async def _regenerate(draft_session=None, channel=None):
    """Run a regeneration and hand back (manager, channel)."""
    mgr = make_manager(draft_id="OLDID000")
    mgr.socket_client.connected = False
    if channel is None:
        channel = MagicMock()
        channel.send = AsyncMock()
    mgr._get_draft_channel = AsyncMock(return_value=channel)
    mgr._get_draft_session_from_db = AsyncMock(
        return_value=_draft_session() if draft_session is None else draft_session)

    with patch("services.draft_setup_manager.db_session", _swap_succeeds()):
        assert await mgr.regenerate_draft_session() is True
    return mgr, channel


def _posted(channel):
    args = " ".join(str(a) for a in channel.send.await_args.args)
    return args + " " + " ".join(str(v) for v in channel.send.await_args.kwargs.values())


@pytest.mark.asyncio
async def test_regenerating_announces_the_move_in_the_draft_channel():
    _, channel = await _regenerate()
    channel.send.assert_awaited()


@pytest.mark.asyncio
async def test_the_announcement_carries_the_new_session_not_the_dead_one():
    mgr, channel = await _regenerate()
    posted = _posted(channel)

    assert mgr.draft_id in posted, "players cannot reach a room the notice never names"
    assert "OLDID000" not in posted, "the dead session must not be offered again"


@pytest.mark.asyncio
async def test_every_signed_up_player_is_pinged_with_their_own_link():
    _, channel = await _regenerate()
    posted = _posted(channel)

    for discord_id, name in (("11", "Alice"), ("22", "Bob")):
        assert f"<@{discord_id}>" in posted
        assert f"userName={name}" in posted


@pytest.mark.asyncio
async def test_only_the_signed_up_players_can_be_pinged():
    """Draftmancer takes any setUserName a client sends and those names are
    interpolated into this message, so "@everyone" is a name a player can pick."""
    _, channel = await _regenerate()
    allowed = channel.send.await_args.kwargs.get("allowed_mentions")

    assert allowed is not None, "an unrestricted send lets a crafted name ping @everyone"
    assert allowed.everyone is False
    assert sorted(u.id for u in allowed.users) == [11, 22]


@pytest.mark.asyncio
async def test_a_failed_announcement_does_not_fail_the_regeneration():
    """The room swap is already committed when this runs, so losing the notice must
    not report the regeneration as failed and strand the manager mid-swap."""
    mgr = make_manager(draft_id="OLDID000")
    mgr.socket_client.connected = False
    mgr._get_draft_channel = AsyncMock(side_effect=RuntimeError("discord down"))
    mgr._get_draft_session_from_db = AsyncMock(return_value=_draft_session())

    with patch("services.draft_setup_manager.db_session", _swap_succeeds()):
        assert await mgr.regenerate_draft_session() is True


@pytest.mark.asyncio
async def test_a_refused_swap_announces_nothing():
    """When the compare-and-swap changes no row the room was NOT replaced, so an
    announcement would send players away from a draft that is still live."""
    mgr = make_manager(draft_id="OLDID000")
    mgr.socket_client.connected = False
    channel = MagicMock()
    channel.send = AsyncMock()
    mgr._get_draft_channel = AsyncMock(return_value=channel)
    mgr._get_draft_session_from_db = AsyncMock(return_value=_draft_session())

    with patch("services.draft_setup_manager.db_session", fake_db_session(rowcount=0)):
        assert await mgr.regenerate_draft_session() is False

    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_draft_with_nobody_signed_up_announces_nothing():
    """Regeneration in an empty queue is routine repair, not an incident."""
    _, channel = await _regenerate(
        draft_session=SimpleNamespace(sign_ups={}, draft_link="x",
                                      draft_channel_id="1543769252744528028",
                                      get_draft_link_for_user=lambda n: "x"))
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_announcement_finds_the_channel_from_the_session_row():
    """self.draft_channel_id starts as None and is filled in by other paths that a
    regenerating manager need not have taken -- set_bot_instance, a ready check, a
    command. _get_draft_channel returns None on an unset id, so the announcement
    would log "No draft_channel_id set" and skip, silently leaving every player on a
    dead link. The row this method already reads carries the channel id."""
    mgr = make_manager(draft_id="OLDID000")
    mgr.socket_client.connected = False
    mgr.draft_channel_id = None                      # never populated

    row = _draft_session()
    row.draft_channel_id = "1543769252744528028"
    mgr._get_draft_session_from_db = AsyncMock(return_value=row)

    channel = MagicMock()
    channel.send = AsyncMock()
    seen = {}

    async def _get_channel():
        seen["id"] = mgr.draft_channel_id
        return channel if mgr.draft_channel_id else None

    mgr._get_draft_channel = _get_channel

    with patch("services.draft_setup_manager.db_session", _swap_succeeds()):
        assert await mgr.regenerate_draft_session() is True

    assert seen.get("id") == "1543769252744528028", "the channel id was never adopted"
    channel.send.assert_awaited()
