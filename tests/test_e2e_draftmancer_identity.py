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
from fake_draftmancer import FakeDraftmancer, attach


async def _connect(manager, server):
    """Take a manager through its real connect path onto the fake server."""
    attach(manager, server)
    return await manager.connect_to_new_session()


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
