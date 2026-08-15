"""A player dropping mid-draft stalls the table, so the bot pauses and says so.

Draftmancer keeps a disconnected player's seat: Session.remUser moves them into
disconnectedUsers and stops only THEIR countdown, so everyone else drafts on until
a pack reaches the empty seat and the table waits — with no timer running out to
end it. DraftBot sessions are not `managed`, so the 30-second replace-with-bots
timeout in remUser never applies to us; the stall is open-ended.

/pause, /unpause and /replace_with_bots already exist (cogs/draft_control.py) — what
was missing is that a human had to NOTICE the drop and run /pause. That is what
these tests pin down: the bot pauses on the event Draftmancer already sends, and
asks the player in Discord to come back.

Resume stays human, through the existing /unpause and its ready check, with one
exception: a drop that resolves before the notice is even posted resumes itself.
Nobody was told, so nobody needs to act — and it means a brief blip cannot leave a
table silently paused waiting on a command no one knows to run.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import make_manager


def _autopause(enabled):
    return patch("services.draft_setup_manager.is_disconnect_autopause_enabled",
                 return_value=enabled)


@pytest.fixture(autouse=True)
def _flag_on():
    """Everything below describes the bot with DISCONNECT_AUTOPAUSE=true.

    It ships off: Draftmancer only delivers userDisconnected to a session's
    non-playing owner and no build has ever subscribed to it, so the event is
    unobserved in production. Shadow mode — the shipping default — has its own tests
    at the end, and they are the ones that matter until the logs say otherwise.
    """
    with _autopause(True):
        yield

GREGG = {"id-gregg": {"userName": "gregg / keezles"}}
GREGG_AND_LSV = {**GREGG, "id-lsv": {"userName": "LSV"}}
SIGN_UPS = {"discord-gregg": "gregg / keezles", "discord-lsv": "LSV"}


def _manager(*, notice_delay=30):
    mgr = make_manager()
    mgr.logger = MagicMock()
    mgr.drafting = True
    # Real time would make these tests wait; the delay itself is not what they check.
    mgr.disconnect_notice_delay = notice_delay

    channel = MagicMock()
    channel.send = AsyncMock()
    mgr._get_draft_channel = AsyncMock(return_value=channel)

    row = MagicMock()
    row.sign_ups = dict(SIGN_UPS)
    mgr._get_draft_session_from_db = AsyncMock(return_value=row)
    return mgr


def _channel(mgr):
    return mgr._get_draft_channel.return_value


def _logged(mgr):
    """The manager's own log lines. loguru does not route through caplog, so the
    shadow run is graded by replacing the bound logger outright."""
    return " ".join(str(c.args[0]) for c in mgr.logger.info.call_args_list)


def _emitted(mgr):
    return [call.args[0] for call in mgr.socket_client.emit.await_args_list]


async def _drop(mgr, disconnected):
    await mgr._on_user_disconnected({"owner": "id-bot", "disconnectedUsers": disconnected})


async def _everyone_back(mgr):
    """How the LAST player returning actually reaches the bot.

    Not `_drop(mgr, {})`. Session.reconnectUser deletes the user from
    disconnectedUsers and only calls broadcastDisconnectedUsers() while someone is
    still missing; when the map empties it calls resumeOnReconnection instead. An
    empty userDisconnected payload is never sent, so testing with one tested a
    situation that cannot occur.
    """
    await mgr._on_resume_on_reconnection({"title": "Player reconnected", "text": "..."})


async def _let_the_notice_run(mgr):
    """Await the pending notice instead of sleeping, so the test is deterministic."""
    if mgr._disconnect_notice_task:
        await mgr._disconnect_notice_task


@pytest.mark.asyncio
async def test_a_mid_draft_disconnect_pauses_the_draft():
    mgr = _manager()

    await _drop(mgr, GREGG)

    assert "pauseDraft" in _emitted(mgr)
    assert mgr.draftPaused is True


@pytest.mark.asyncio
async def test_a_blip_that_resolves_before_the_notice_resumes_itself_silently():
    """The one case the bot resumes on its own. Nobody was told the draft paused, so
    leaving it paused would strand the table waiting on an /unpause no one knows to
    run."""
    mgr = _manager(notice_delay=30)  # long enough that nothing is ever posted

    await _drop(mgr, GREGG)
    await _everyone_back(mgr)  # back again

    assert _emitted(mgr) == ["pauseDraft", "resumeDraft"]
    assert mgr.draftPaused is False
    _channel(mgr).send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_disconnect_that_outlasts_the_window_asks_them_to_reconnect():
    mgr = _manager(notice_delay=0)

    await _drop(mgr, GREGG)
    await _let_the_notice_run(mgr)

    _channel(mgr).send.assert_awaited_once()
    message = _channel(mgr).send.await_args.args[0]
    # a mention, so the person who has to act actually gets pinged
    assert "<@discord-gregg>" in message
    assert "/unpause" in message


@pytest.mark.asyncio
async def test_a_return_after_the_notice_leaves_the_pause_standing():
    """Once players have been told, resuming is theirs to do — /unpause runs a ready
    check, which is the point of making it manual."""
    mgr = _manager(notice_delay=0)

    await _drop(mgr, GREGG)
    await _let_the_notice_run(mgr)
    await _everyone_back(mgr)

    assert "resumeDraft" not in _emitted(mgr)
    assert mgr.draftPaused is True


@pytest.mark.asyncio
async def test_a_pause_the_bot_did_not_start_is_never_auto_resumed():
    """A participant's /pause is not the bot's to undo, whatever happens to the
    connection afterwards."""
    mgr = _manager(notice_delay=30)
    mgr.draftPaused = True  # someone ran /pause first

    await _drop(mgr, GREGG)
    await _everyone_back(mgr)

    assert "resumeDraft" not in _emitted(mgr)
    assert mgr.draftPaused is True


@pytest.mark.asyncio
async def test_a_second_player_dropping_does_not_pause_or_announce_again():
    mgr = _manager(notice_delay=0)

    await _drop(mgr, GREGG)
    await _drop(mgr, GREGG_AND_LSV)
    await _let_the_notice_run(mgr)

    assert _emitted(mgr).count("pauseDraft") == 1
    _channel(mgr).send.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_duplicate_display_name_is_named_but_not_mentioned():
    """sign_ups is keyed by discord id and two players can share a display name, so a
    name lookup can pick the wrong person. Better an unpinged right name than a
    pinged wrong one."""
    mgr = _manager(notice_delay=0)
    row = MagicMock()
    row.sign_ups = {"discord-sam-1": "Sam", "discord-sam-2": "Sam"}
    mgr._get_draft_session_from_db = AsyncMock(return_value=row)

    await _drop(mgr, {"id-sam": {"userName": "Sam"}})
    await _let_the_notice_run(mgr)

    message = _channel(mgr).send.await_args.args[0]
    assert "Sam" in message
    assert "<@discord-sam-1>" not in message and "<@discord-sam-2>" not in message


@pytest.mark.asyncio
async def test_a_notice_that_fails_to_send_still_leaves_the_pause_standing():
    """If Discord refuses the message the players are stuck with a paused draft and no
    explanation — bad, but recoverable with /unpause. Silently resuming instead would
    put the table back under way while someone is still missing, which is the state
    this whole feature exists to avoid."""
    mgr = _manager(notice_delay=0)
    _channel(mgr).send.side_effect = RuntimeError("missing permissions")

    await _drop(mgr, GREGG)
    await _let_the_notice_run(mgr)
    await _everyone_back(mgr)

    assert "resumeDraft" not in _emitted(mgr)
    assert mgr.draftPaused is True


@pytest.mark.asyncio
async def test_the_draft_ending_drops_a_pending_notice():
    """The draft can end while someone is still disconnected — /scrap, or the others
    replacing them with bots and finishing. Posting "please reconnect" after that is
    noise about a draft that is over."""
    mgr = _manager(notice_delay=30)
    mgr.draft_cancelled = True  # the /scrap path, which skips the rooms-and-pairings work
    await _drop(mgr, GREGG)
    task = mgr._disconnect_notice_task

    await mgr._on_end_draft({})

    await asyncio.sleep(0)  # let the cancellation land
    assert task.cancelled() or task.done()
    _channel(mgr).send.assert_not_awaited()


# ---- shadow mode: the shipping default -------------------------------------------


@pytest.mark.asyncio
async def test_shadow_mode_touches_neither_draftmancer_nor_discord():
    """The whole point of shipping off: work out the decision, do none of it.

    draftPaused is checked explicitly because it is not private bookkeeping — the
    seating recovery reads it and /unpause refuses to run without it. Setting it
    without a real pause would leave the rest of the bot believing a draft was paused
    while Draftmancer played on, which is a worse failure than the one being guarded
    against.
    """
    mgr = _manager(notice_delay=0)

    with _autopause(False):
        await _drop(mgr, GREGG)
        await _let_the_notice_run(mgr)

    assert _emitted(mgr) == [], "shadow mode must not talk to Draftmancer"
    _channel(mgr).send.assert_not_awaited()
    assert mgr.draftPaused is False


@pytest.mark.asyncio
async def test_shadow_mode_still_renders_the_notice_it_would_have_sent():
    """Rendering it is what proves the channel lookup, the sign_ups read and the
    mention resolution work — before any of it is trusted with a live draft."""
    mgr = _manager(notice_delay=0)

    with _autopause(False):
        await _drop(mgr, GREGG)
        await _let_the_notice_run(mgr)

    logged = _logged(mgr)
    assert "[autopause-shadow]" in logged
    assert "<@discord-gregg>" in logged, "the shadow log should carry the real message"


@pytest.mark.asyncio
async def test_shadow_mode_records_how_long_the_disconnect_lasted():
    """The number that decides whether 15s is anywhere near right. Nothing has ever
    measured it, because nothing subscribed to the event that reports it."""
    mgr = _manager(notice_delay=30)

    with _autopause(False):
        await _drop(mgr, GREGG)
        await _everyone_back(mgr)

    logged = _logged(mgr)
    assert "would resume" in logged
    assert "lasted" in logged


def test_the_flag_ships_off():
    """The guarantee that deploying this stack changes nothing for players.

    Asserted against the real config function rather than the patch used above, so
    that a mistake in the default cannot hide behind the test fixture.
    """
    import os

    from config import is_disconnect_autopause_enabled

    with patch.dict(os.environ):
        os.environ.pop("DISCONNECT_AUTOPAUSE", None)
        assert is_disconnect_autopause_enabled() is False

        os.environ["DISCONNECT_AUTOPAUSE"] = "true"
        assert is_disconnect_autopause_enabled() is True


# ---- what the Codex review caught ------------------------------------------------


@pytest.mark.asyncio
async def test_the_real_reconnect_event_resumes_a_blip():
    """The bug the old tests hid: they fed `userDisconnected: {}`, which upstream
    never sends. In production the blip would never have been noticed, the notice
    would have fired 15s later for a player already back, and the draft would have
    stayed paused waiting on an /unpause nobody expected to need."""
    mgr = _manager(notice_delay=30)

    await _drop(mgr, GREGG)
    await _everyone_back(mgr)

    assert _emitted(mgr) == ["pauseDraft", "resumeDraft"]
    assert mgr.draftPaused is False
    assert mgr.disconnected_users == {}


@pytest.mark.asyncio
async def test_a_later_blip_cannot_resume_a_pause_handed_to_the_players():
    """Once the room has been told to use /unpause, that is the only way back — a
    second brief drop must not quietly hand the draft back to the bot's own
    resume path and lift a pause a ready check was supposed to lift."""
    mgr = _manager(notice_delay=0)

    await _drop(mgr, GREGG)
    await _let_the_notice_run(mgr)      # players told; resume is theirs now
    await _everyone_back(mgr)
    assert mgr.draftPaused is True

    # ...and later someone drops briefly again while the draft is still paused
    mgr.disconnect_notice_delay = 30
    await _drop(mgr, GREGG)
    await _everyone_back(mgr)

    assert "resumeDraft" not in _emitted(mgr)
    assert mgr.draftPaused is True


@pytest.mark.asyncio
async def test_a_notice_mid_send_is_not_cancelled():
    """_disconnect_notice_sent flips the moment the grace window elapses, so between
    that and the message landing there is a window where cancelling would leave the
    flag claiming the room was told while nothing was ever posted — a paused draft
    and no explanation.

    Suspends the task INSIDE channel.send to sit in that window deliberately; an
    earlier version of this test let the task finish first, so `not task.done()`
    short-circuited and it proved nothing.
    """
    mgr = _manager(notice_delay=0)
    release = asyncio.Event()

    async def blocking_send(*args, **kwargs):
        await release.wait()

    _channel(mgr).send = AsyncMock(side_effect=blocking_send)

    await _drop(mgr, GREGG)
    for _ in range(10):          # let it clear the sleep and reach the send
        await asyncio.sleep(0)
    assert mgr._disconnect_notice_sent is True, "should be past the point of no return"
    task = mgr._disconnect_notice_task
    assert not task.done(), "should be suspended inside the send"

    mgr._cancel_disconnect_notice()
    await asyncio.sleep(0)

    assert not task.cancelled(), "cancelling mid-send drops the message but keeps the flag"

    release.set()
    await task
    _channel(mgr).send.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_manager_being_torn_down_stops_talking():
    """/mutiny hands the session to a human and drops the manager. A notice still
    pending from before that would post about a draft this manager no longer runs."""
    mgr = _manager(notice_delay=30)
    mgr.socket_client.connected = False
    await _drop(mgr, GREGG)
    task = mgr._disconnect_notice_task

    await mgr._cleanup_and_disconnect("mutiny command")

    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    _channel(mgr).send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_player_named_everyone_cannot_ping_the_server():
    """Draftmancer accepts any setUserName, and an unresolved name is inserted as raw
    text. Without allowed_mentions the bot would happily @everyone on their behalf."""
    mgr = _manager(notice_delay=0)
    row = MagicMock()
    row.sign_ups = {"discord-someone": "someone else"}   # no match -> raw name used
    mgr._get_draft_session_from_db = AsyncMock(return_value=row)

    await _drop(mgr, {"id-x": {"userName": "@everyone"}})
    await _let_the_notice_run(mgr)

    kwargs = _channel(mgr).send.await_args.kwargs
    mentions = kwargs.get("allowed_mentions")
    assert mentions is not None, "raw Draftmancer names need an allowed_mentions guard"
    assert mentions.everyone is False and mentions.roles is False


def test_the_pause_command_can_resolve_the_shared_copy():
    """/pause interpolates PAUSED_DRAFT_OPTIONS into its reply. Sharing the constant
    without importing it left the command raising NameError *after* it had already
    paused Draftmancer — the draft stops, the player sees an error. Nothing else in
    the suite exercises pause_command, so this asserts the name resolves in the
    module that uses it."""
    import cogs.draft_control as draft_control

    assert isinstance(getattr(draft_control, "PAUSED_DRAFT_OPTIONS", None), str)
    assert "/unpause" in draft_control.PAUSED_DRAFT_OPTIONS
