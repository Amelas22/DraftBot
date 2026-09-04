"""When the bot moves a draft to a new Draftmancer room, it says so -- but only
to people who were given a link to the old one.

frontier-guide-69 (2026-09-02) regenerated five seconds after the queue opened.
Twelve minutes later the bot's own session held zero non-bot users while six
players sat in the abandoned room; the draft was given up on and re-run by hand in
a session a player made themselves. Links already in hands cannot be edited, so
the response was an announcement carrying a fresh personalised link each.

What that response got wrong, and these tests now pin: it assumed players hold a
link from sign-up. They do not. The sign-up confirmation says "Your Draftmancer
link will be provided once teams are created" (views.py), and no other pre-teams
surface renders one -- not the ready-check DM, not the sign-up embed. Links reach
players in exactly one transaction, the one team creation commits.

So the announcement fired only ever during sign-up -- regenerate_draft_session's
compare-and-swap requires session_stage IS NULL -- which is precisely when it is
false in both directions: nobody had a link, and nobody had been in the room it
named. It did that in production on 2026-09-04.

The case that survives is the race the swap cannot close: team creation committing
between that UPDATE and the announcement's own read. There the notice is earned,
and that is the case every test below except the sign-up one describes.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import fake_db_session, make_manager


def _swap_succeeds():
    """The UPDATE reports one row changed -- the room swap happened."""
    return fake_db_session(rowcount=1)


def _draft_session():
    """Two signed-up players, so the announcement has someone to address.

    teams_start_time is set because that is the case the announcement is FOR:
    teams are made and everyone's personalised link is posted in the same
    commit, so from that moment there are links in hands that a swap invalidates.
    """
    ds = SimpleNamespace(sign_ups={"11": "Alice", "22": "Bob"},
                         draft_channel_id="1543769252744528028",
                         teams_start_time=datetime(2026, 9, 4, 11, 0),
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
    # Built from the shared row so the ONLY thing wrong with it is the empty
    # queue. Hand-rolling a namespace here silently omitted teams_start_time,
    # which left this passing on the order the guards happen to run in.
    empty_queue = _draft_session()
    empty_queue.sign_ups = {}

    _, channel = await _regenerate(draft_session=empty_queue)
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_draft_still_in_signup_announces_nothing():
    """Before teams are created nobody has been handed a link, so there is
    nothing stale to correct -- and the notice actively misinforms, telling
    people that a link they were never given has stopped working.

    The pre-existing guards cannot catch this. They ask whether a link can be
    BUILT for each signed-up player, and get_draft_link_for_user just decorates
    draft_link, which exists from session creation. So they were satisfied from
    the moment the queue opened, which is why this fired in production on
    2026-09-04 for a draft that had not started.
    """
    still_in_signup = _draft_session()
    still_in_signup.teams_start_time = None

    _, channel = await _regenerate(draft_session=still_in_signup)

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
