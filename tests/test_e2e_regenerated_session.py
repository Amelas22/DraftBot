"""End-to-end: after a room swap, the players and the bot end up in one place.

The two halves of frontier-guide-69 (2026-09-02) were each survivable on their own.
Together they were not: the bot moved to a new Draftmancer room, and the players
kept a link to the old one, so at 16:11:59 the bot's session reported

    Expected user count from database: 6
    Current non-bot users: []

while six people sat in the room it had walked away from.

So the invariant worth pinning is not "an announcement was sent" -- the unit tests
cover that -- but that the room named in the announcement is the room the bot is
actually connected to. Asserting them separately is what let them drift apart.
"""
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import fake_db_session, make_manager
from fake_draftmancer import FakeDraftmancer, attach


def _swap_succeeds():
    """The UPDATE reports one row changed -- the room swap happened."""
    return fake_db_session(rowcount=1)


def _session_row(manager):
    """A DraftSession whose links track the manager's current draft_id, as the real
    row does -- regenerate_draft_session writes draft_link in the same statement."""
    row = SimpleNamespace(sign_ups={"11": "Alice", "22": "Bob"},
                          draft_channel_id="1543769252744528028")
    row.get_draft_link_for_user = (
        lambda name: f"https://draftmancer.com/?session=DB{manager.draft_id}&userName={name}"
    )
    return row


@pytest.mark.asyncio
async def test_the_announced_room_is_the_room_the_bot_joins():
    server = FakeDraftmancer()
    mgr = make_manager(session_id="s1", draft_id="OLDROOM0")
    attach(mgr, server)
    await mgr.connect_to_new_session()
    assert server.users_in("DBOLDROOM0") == [mgr.bot_user_id]

    channel = MagicMock()
    channel.send = AsyncMock()
    mgr._get_draft_channel = AsyncMock(return_value=channel)
    mgr._get_draft_session_from_db = AsyncMock(side_effect=lambda: _session_row(mgr))

    with patch("services.draft_setup_manager.db_session", _swap_succeeds()):
        assert await mgr.regenerate_draft_session() is True
    assert await mgr.connect_to_new_session() is True

    announced = set(re.findall(r"session=DB([A-Z0-9]{8})",
                               channel.send.await_args.args[0]))
    # Every room holding a bot connection, not just the current identity's -- a
    # zombie left in the old room must fail this too.
    joined = {s.session_id for s in server.connections.values() if s.user_name == "DraftBot"}

    assert announced == {mgr.draft_id}, "players were pointed at more than one room"
    assert joined == {f"DB{mgr.draft_id}"}, (
        "the bot is not in the room it told the players to join")
    assert "OLDROOM0" not in channel.send.await_args.args[0]


@pytest.mark.asyncio
async def test_the_bot_leaves_the_room_it_told_players_to_abandon():
    """A bot still sitting in the dead room keeps it looking alive to anyone who
    wanders in with an old link."""
    server = FakeDraftmancer()
    mgr = make_manager(session_id="s1", draft_id="OLDROOM0")
    attach(mgr, server)
    await mgr.connect_to_new_session()

    channel = MagicMock()
    channel.send = AsyncMock()
    mgr._get_draft_channel = AsyncMock(return_value=channel)
    mgr._get_draft_session_from_db = AsyncMock(side_effect=lambda: _session_row(mgr))

    with patch("services.draft_setup_manager.db_session", _swap_succeeds()):
        await mgr.regenerate_draft_session()
    await mgr.connect_to_new_session()

    assert server.users_in("DBOLDROOM0") == []


@pytest.mark.asyncio
async def test_a_regenerated_bot_does_not_collide_with_its_own_zombie_socket():
    """Regeneration runs precisely when the connection has gone wrong, so the old
    socket may still be registered server-side while the client considers it gone.

    Reconnecting under the identity derived from the OLD draft_id then walks into
    Draftmancer's duplicate-login path against the bot's own dead socket -- the same
    branch that evicts and renames, now self-inflicted. Moving the identity with the
    draft_id is what avoids it, and this is the case that makes that line matter:
    after a clean disconnect nothing holds the old id and a stale one is merely
    untidy.
    """
    server = FakeDraftmancer()
    mgr = make_manager(session_id="s1", draft_id="OLDROOM0")
    attach(mgr, server)
    await mgr.connect_to_new_session()

    # The socket drops without the server reaping it: the client sees it as gone,
    # `Connections` still holds the id.
    mgr.socket_client.socket.connected = False

    mgr._get_draft_channel = AsyncMock(return_value=None)
    mgr._get_draft_session_from_db = AsyncMock(side_effect=lambda: _session_row(mgr))
    with patch("services.draft_setup_manager.db_session", _swap_succeeds()):
        assert await mgr.regenerate_draft_session() is True
    assert await mgr.connect_to_new_session() is True

    assert "duplicate userID" not in " ".join(server.log), (
        "the bot re-entered the duplicate-login path against its own dead socket")
    assert mgr.socket_client.connected
