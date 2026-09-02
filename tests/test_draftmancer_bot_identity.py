"""The bot tracks who it is to Draftmancer, and lets Draftmancer rename it.

Draftmancer keys live connections globally by userID (src/server.ts, `Connections`).
Its duplicate-login path has two branches, and the bot could survive neither because
it had no notion of its own identity -- it passed the literal string "DraftBot" at
the call site and never looked at it again:

  - if the socket already holding that userID does not answer "stillAlive" within
    3s, the server closes it and accepts the newcomer;
  - if it does answer, the server accepts the newcomer under a freshly generated
    UUID and announces it as "alreadyConnected".

That second branch is why ownership reclaim failed in production with
"Cannot reclaim ownership - bot userID not found in session_users": Draftmancer had
already renamed the bot, and the bot was still looking for its old name.

This module is the identity itself -- one attribute, used when connecting, updated
when the server reassigns it. What the identity is *derived from* is a separate
concern; here it stays the historical literal so this layer changes no behaviour.

No "stillAlive" handler is registered, deliberately. Answering it would keep a
half-open socket alive and demote our new connection to a random UUID; staying
silent is what lets the server reap the dead socket and hand the identity back.
"""
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import make_manager
# The subscription probe lives with the subscription-name contract it serves.
from test_socket_event_names import _subscribed_events


def test_the_manager_knows_its_own_draftmancer_identity():
    """Held as state rather than passed inline, which is what lets it be reassigned."""
    assert make_manager(draft_id="AAAA1111").bot_user_id == "DraftBot"


def test_already_connected_is_subscribed():
    """Without this the bot never learns Draftmancer renamed it."""
    assert "alreadyConnected" in _subscribed_events()


def test_still_alive_is_not_subscribed():
    """Answering it keeps a half-open socket and demotes our new connection to a
    random UUID. Silence is what lets the server reap the dead socket."""
    assert "stillAlive" not in _subscribed_events()


@pytest.mark.asyncio
async def test_already_connected_adopts_the_reassigned_user_id():
    """The payload is the new userID. Ownership and seating are addressed by it, so
    a bot still calling itself the old name cannot find itself in sessionUsers."""
    mgr = make_manager(draft_id="AAAA1111")

    await mgr._on_already_connected("3f8a-uuid-from-server")

    assert mgr.bot_user_id == "3f8a-uuid-from-server"


@pytest.mark.asyncio
async def test_connecting_uses_the_identity_the_manager_is_holding():
    """The identity only helps if it reaches the wire, including after a rename."""
    mgr = make_manager(draft_id="NEWID111")
    mgr.bot_user_id = "reassigned-uuid-from-server"
    mgr.socket_client.connect_with_retry = AsyncMock(return_value=True)

    with patch("services.draft_setup_manager.get_draftmancer_websocket_url") as url:
        url.return_value = "ws://x"
        await mgr.connect_to_new_session()

    assert url.call_args.kwargs.get("user_id") == "reassigned-uuid-from-server"


def test_the_bot_is_still_named_DraftBot_on_the_wire():
    """Every bot-vs-player filter in the manager compares userName, never userID, so
    the display name must not move when the id does."""
    from config import get_draftmancer_websocket_url

    query = parse_qs(urlparse(get_draftmancer_websocket_url("AAAA1111")).query)
    assert query["userName"][0] == "DraftBot"
    assert query["sessionID"][0] == "DBAAAA1111"


@pytest.mark.asyncio
async def test_ownership_is_reclaimed_without_waiting_for_the_user_list():
    """Reclaim used to discover its own userID by scanning sessionUsers for the name
    "DraftBot", behind a retry-and-sleep loop, and gave up when that payload had not
    arrived -- the production error was literally

        Cannot reclaim ownership - bot userID not found in session_users

    which is the state a flapping socket is in constantly. The manager now knows who
    it is, so the answer needs no payload and no waiting.
    """
    from unittest.mock import AsyncMock

    mgr = make_manager(draft_id="AAAA1111")
    mgr.session_users = []                      # the payload never came
    mgr.socket_client.emit = AsyncMock(return_value=True)

    with patch("services.draft_setup_manager.asyncio.sleep", AsyncMock()):
        assert await mgr._reclaim_ownership_as_spectator() is True

    owner_calls = [c for c in mgr.socket_client.emit.await_args_list
                   if c.args and c.args[0] == "setSessionOwner"]
    assert owner_calls, "never tried to claim ownership"
    assert owner_calls[0].args[1] == mgr.bot_user_id


@pytest.mark.asyncio
async def test_ownership_is_reclaimed_under_a_reassigned_identity():
    """After Draftmancer renames us, ownership must be claimed under the new id --
    claiming under the old one addresses a user the server no longer has."""
    from unittest.mock import AsyncMock

    mgr = make_manager(draft_id="AAAA1111")
    mgr.session_users = [{"userID": "DraftBot-AAAA1111", "userName": "DraftBot"}]
    mgr.socket_client.emit = AsyncMock(return_value=True)
    await mgr._on_already_connected("uuid-reassigned")

    with patch("services.draft_setup_manager.asyncio.sleep", AsyncMock()):
        assert await mgr._reclaim_ownership_as_spectator() is True

    owner_calls = [c for c in mgr.socket_client.emit.await_args_list
                   if c.args and c.args[0] == "setSessionOwner"]
    assert owner_calls[0].args[1] == "uuid-reassigned"
