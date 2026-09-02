"""End-to-end: a real manager against a server that enforces Draftmancer's rules.

The unit tests next door call `_on_already_connected` directly, which proves the
handler works but assumes the event ever arrives. These drive the real connect
path against tests/fake_draftmancer.py, whose connection rules are transcribed
from Draftmancer's src/server.ts -- so the server decides whether the bot gets
renamed, evicted or accepted, exactly as production does.

That distinction is what these are for. The production failure was not a handler
misbehaving; it was the bot losing an argument with the server about who it was.
"""
import pytest

from conftest import make_manager
from fake_draftmancer import FakeDraftmancer, attach, connect as _connect


@pytest.mark.asyncio
async def test_a_lone_bot_connects_and_is_listed_in_its_session():
    """The baseline. If this breaks, the harness is wrong, not the bot."""
    server = FakeDraftmancer()
    mgr = make_manager(draft_id="AAAA1111")

    assert await _connect(mgr, server) is True
    assert server.users_in("DBAAAA1111") == [mgr.bot_user_id]


@pytest.mark.asyncio
async def test_the_bot_adopts_the_identity_draftmancer_gives_it():
    """Something else already holds our userID and answers the server's liveness
    ping, so Draftmancer accepts us under a generated uuid instead. The bot has to
    come away knowing the new id -- ownership and seating are addressed by it.

    A stale socket from a previous bot process is the realistic cause: it is still
    open enough to answer, so the server takes its rename branch rather than
    evicting it.
    """
    server = FakeDraftmancer()
    mgr = make_manager(draft_id="AAAA1111")
    squatter = server.add_squatter(mgr.bot_user_id, answers=True)

    assert await _connect(mgr, server) is True

    assert mgr.bot_user_id != squatter.user_id, "still using an id the server took away"
    assert mgr.bot_user_id.startswith("uuid-")
    assert mgr.bot_user_id in server.users_in("DBAAAA1111"), (
        "the bot cannot find itself in sessionUsers under the id it thinks it has, "
        "which is what makes ownership reclaim fail"
    )


@pytest.mark.asyncio
async def test_the_bot_does_not_answer_the_servers_liveness_ping():
    """Answering keeps a half-open socket alive and demotes the new connection to a
    generated uuid. Silence is what lets the server reap a dead socket and hand the
    identity back, which is what a genuine reconnect depends on.

    Asserted against the server rather than the subscription list, so it holds even
    if the handler arrives by some other route.
    """
    server = FakeDraftmancer()
    mgr = make_manager(draft_id="AAAA1111")
    await _connect(mgr, server)

    assert server.socket_for(mgr.bot_user_id).answers_still_alive() is False


@pytest.mark.asyncio
async def test_a_reconnect_reclaims_an_identity_a_dead_socket_still_holds():
    """The flip side of not answering: a manager whose socket died without the
    server noticing must be able to take its own id back."""
    server = FakeDraftmancer()
    mgr = make_manager(draft_id="AAAA1111")
    await _connect(mgr, server)
    stale = server.socket_for(mgr.bot_user_id)

    # Reconnect without a clean disconnect, exactly as a dropped socket would.
    assert await _connect(mgr, server) is True

    assert stale.connected is False, "the dead socket was left holding the identity"
    assert server.users_in("DBAAAA1111") == [mgr.bot_user_id]


@pytest.mark.asyncio
async def test_a_second_disconnect_reconnects_to_where_the_draft_moved():
    """After a regeneration the reconnect loop must follow the draft.

    keep_connection_alive built the connect URL once, before the loop, and handed
    that same string to every reconnect. But _handle_reconnection can itself move
    the draft -- it calls regenerate_draft_session, which swaps draft_id and, now
    that identity is state, bot_user_id too. The next drop therefore went back to
    the room the bot had just abandoned, under an identity the server may have
    taken away.

    The first drop regenerates; the second is the one that used to go backwards.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    server = FakeDraftmancer()
    mgr = make_manager(session_id="s1", draft_id="OLDROOM0")
    attach(mgr, server)
    await mgr.connect_to_new_session()

    # Ownership is unrecoverable and the queue is still open: the path that
    # regenerates rather than preserving.
    mgr._reclaim_ownership_as_spectator = AsyncMock(return_value=False)
    mgr.must_preserve_draft_room = AsyncMock(return_value=False)
    mgr._get_draft_channel = AsyncMock(return_value=None)
    mgr._get_draft_session_from_db = AsyncMock(return_value=None)

    swap = MagicMock()
    swap.return_value.__aenter__ = AsyncMock(return_value=MagicMock(
        execute=AsyncMock(return_value=MagicMock(rowcount=1)), commit=AsyncMock()))
    swap.return_value.__aexit__ = AsyncMock(return_value=None)

    with patch("services.draft_setup_manager.db_session", swap):
        assert await mgr._handle_reconnection() is True
    moved_to = f"DB{mgr.draft_id}"
    assert moved_to != "DBOLDROOM0"

    # Drop again. This reconnect must target the room the draft moved to.
    await server.disconnect(mgr.socket_client.socket)
    mgr._reclaim_ownership_as_spectator = AsyncMock(return_value=True)
    assert await mgr._handle_reconnection() is True

    assert mgr.socket_client.socket.session_id == moved_to, (
        "the reconnect went back to the room the bot had abandoned")
    assert mgr.socket_client.socket.user_id == mgr.bot_user_id


@pytest.mark.asyncio
async def test_ownership_survives_a_reconnect_after_the_bot_became_a_spectator():
    """The bot removes itself from the payload it used to identify itself with.

    Reclaiming ownership ends in setOwnerIsPlayer(False). Draftmancer's handler
    deletes the owner from `sess.users` (src/server.ts:585) and sessionUsers is
    built from users + disconnectedUsers (src/Session.ts:3747), so from that moment
    the bot is absent from every sessionUsers payload while still being connected.

    Discovering its own id by scanning that payload for "DraftBot" was therefore
    not merely fragile -- it could never succeed again after the first reclaim.
    Every later reconnect logged "bot userID not found in session_users".
    """
    server = FakeDraftmancer()
    mgr = make_manager(session_id="s1", draft_id="AAAA1111")
    await _connect(mgr, server)

    assert await mgr._reclaim_ownership_as_spectator() is True
    assert server.owners["DBAAAA1111"] == mgr.bot_user_id
    assert [u["userID"] for u in server.session_user_payload("DBAAAA1111")] == [], (
        "the harness is not modelling the owner leaving sess.users")

    # Reconnect and reclaim again -- the state every later reconnect starts from.
    await server.disconnect(mgr.socket_client.socket)
    await _connect(mgr, server)

    assert await mgr._reclaim_ownership_as_spectator() is True
    assert server.owners["DBAAAA1111"] == mgr.bot_user_id


@pytest.mark.asyncio
async def test_two_drafts_being_set_up_at_once_both_keep_their_connection():
    """The production incident, reproduced.

    frontier-guide-69 (2026-09-02) was queued while keldon-mantle-96 was already
    connected. Sharing one userID made them a single duplicate-logging-in user, so
    the server evicted whichever socket did not answer its liveness ping -- neither
    of them, ever. Each manager's reconnect loop then took the identity back from
    the other, and the newcomer never held a socket long enough for the 30s
    importCube callback. Six players were sent to a session with no cube in it.

    Nothing here is about drafting; it is entirely about whether two managers can
    coexist, which is the thing that was untestable without a server.
    """
    server = FakeDraftmancer()
    first = make_manager(session_id="s1", draft_id="KELDON96")
    second = make_manager(session_id="s2", draft_id="FRONTIER")

    assert await _connect(first, server) is True
    assert await _connect(second, server) is True

    assert first.socket_client.connected, "the older draft was evicted by the newer one"
    assert second.socket_client.connected
    assert server.users_in("DBKELDON96") == [first.bot_user_id]
    assert server.users_in("DBFRONTIER") == [second.bot_user_id]
    assert "evicted" not in " ".join(server.log)


@pytest.mark.asyncio
async def test_a_draft_starting_mid_setup_does_not_disturb_the_others():
    """The eviction was not a two-party problem. Managers reconnect on a loop, so a
    third draft opening is another chance to take everyone else's identity."""
    server = FakeDraftmancer()
    managers = [make_manager(session_id=f"s{i}", draft_id=f"DRAFT{i:03d}") for i in range(5)]

    for mgr in managers:
        assert await _connect(mgr, server) is True

    assert all(m.socket_client.connected for m in managers)
    assert len({m.bot_user_id for m in managers}) == len(managers)
