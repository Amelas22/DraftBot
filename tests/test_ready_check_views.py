"""Tests for the view/utility wiring around the lobby ready check.

Covers PersistentView.ready_check_callback (guards + happy path + re-fire),
the startup button-strip helper, and the timeout/debounce ordering invariant.
All Discord/DB I/O is mocked — no live bot or database needed.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import utils
import views
from ready_check import ReadyCheckSession
from views import PersistentView, READY_CHECK_DEBOUNCE_SECONDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _view(session_id="sid"):
    """A PersistentView without running __init__ (skips real button construction)."""
    v = PersistentView.__new__(PersistentView)
    v.draft_session_id = session_id
    return v


def _interaction(user_id="0"):
    i = MagicMock()
    i.user.id = user_id
    i.response.send_message = AsyncMock()
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock(return_value=MagicMock(id=12345))
    i.channel = MagicMock()
    i.channel.id = "100"
    i.channel.name = "draft-chan"
    i.guild = MagicMock()
    i.guild.id = "42"
    i.guild.name = "Guild"
    i.client = MagicMock()
    return i


def _session(n_signups=6):
    s = MagicMock()
    s.sign_ups = {str(i): f"P{i}" for i in range(n_signups)}
    return s


def _mock_async_session():
    """Mimics `async with AsyncSessionLocal() as s: async with s.begin(): await s.execute(...)`."""
    session = MagicMock()
    session.execute = AsyncMock()
    begin = MagicMock()
    begin.__aenter__ = AsyncMock(return_value=session)
    begin.__aexit__ = AsyncMock(return_value=None)
    session.begin = MagicMock(return_value=begin)
    outer = MagicMock()
    outer.__aenter__ = AsyncMock(return_value=session)
    outer.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=outer), session


def _make_create_task_stub():
    """Stand-in for asyncio.create_task that records and safely closes coroutines."""
    calls = []

    def stub(coro):
        calls.append(coro)
        if asyncio.iscoroutine(coro):
            coro.close()
        return MagicMock()

    stub.calls = calls
    return stub


# ---------------------------------------------------------------------------
# ready_check_callback — guard clauses (B1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestReadyCheckCallbackGuards:
    async def test_cooldown_blocks_and_does_not_start(self):
        view = _view()
        i = _interaction()
        with patch("views.state_manager") as sm, \
             patch("views.get_draft_session", AsyncMock(return_value=_session(6))):
            sm.get_cooldown.return_value = datetime.now() + timedelta(seconds=90)
            await view.ready_check_callback(i, MagicMock())

        sm.set_ready_check.assert_not_called()
        sm.set_cooldown.assert_not_called()
        i.response.send_message.assert_awaited_once()
        assert "cooldown" in i.response.send_message.call_args.args[0].lower()

    async def test_under_six_players_rejected(self):
        view = _view()
        i = _interaction()
        with patch("views.state_manager") as sm, \
             patch("views.get_draft_session", AsyncMock(return_value=_session(3))):
            sm.get_cooldown.return_value = None
            await view.ready_check_callback(i, MagicMock())

        sm.set_ready_check.assert_not_called()
        assert "6 or more" in i.response.send_message.call_args.args[0]

    async def test_non_participant_rejected(self):
        view = _view()
        i = _interaction(user_id="999")  # not among sign_ups 0..5
        with patch("views.state_manager") as sm, \
             patch("views.get_draft_session", AsyncMock(return_value=_session(6))), \
             patch("views.asyncio.create_task", _make_create_task_stub()):
            sm.get_cooldown.return_value = None
            await view.ready_check_callback(i, MagicMock())

        sm.set_ready_check.assert_not_called()
        assert "not registered" in i.response.send_message.call_args.args[0].lower()


# ---------------------------------------------------------------------------
# ready_check_callback — happy path + re-fire (B2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestReadyCheckCallbackStart:
    async def test_happy_path_starts_check_without_disabling_button(self):
        view = _view()
        i = _interaction(user_id="0")
        factory, db_sess = _mock_async_session()
        stub = _make_create_task_stub()
        with patch("views.state_manager") as sm, \
             patch("views.get_draft_session", AsyncMock(return_value=_session(6))), \
             patch("views.AsyncSessionLocal", factory), \
             patch("views.send_ready_check_dms", AsyncMock()), \
             patch("views.asyncio.create_task", stub), \
             patch("ready_check.get_display_name_by_id", lambda uid, g, fb: fb), \
             patch.object(ReadyCheckSession, "handle_all_ready", AsyncMock()) as hac:
            sm.get_cooldown.return_value = None
            sm.has_ready_check.return_value = False
            await view.ready_check_callback(i, MagicMock())

        sm.set_ready_check.assert_called_once()          # the check started
        i.message.edit.assert_not_called()               # button NOT disabled
        db_sess.execute.assert_awaited()                 # message id persisted
        i.followup.send.assert_awaited()                 # ready-check message sent
        assert len(stub.calls) == 2                      # remove_cooldown + run_timeout scheduled
        hac.assert_not_awaited()                          # 6 players, only initiator ready

    async def test_refire_cleans_up_previous_check_silently(self):
        view = _view()
        i = _interaction(user_id="0")
        factory, _ = _mock_async_session()
        with patch("views.state_manager") as sm, \
             patch("views.get_draft_session", AsyncMock(return_value=_session(6))), \
             patch("views.AsyncSessionLocal", factory), \
             patch("views.send_ready_check_dms", AsyncMock()), \
             patch("views.asyncio.create_task", _make_create_task_stub()), \
             patch("ready_check.get_display_name_by_id", lambda uid, g, fb: fb), \
             patch.object(ReadyCheckSession, "handle_all_ready", AsyncMock()), \
             patch.object(ReadyCheckSession, "cleanup", AsyncMock()) as cleanup:
            sm.get_cooldown.return_value = None
            sm.has_ready_check.return_value = True  # a previous check is still active
            await view.ready_check_callback(i, MagicMock())

        cleanup.assert_awaited_once_with("sid", i.channel)


# ---------------------------------------------------------------------------
# utils.strip_stale_lobby_ready_checks (C)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStripStaleLobbyReadyChecks:
    def _stale(self, sid="s1", chan="100", msg="555"):
        s = MagicMock()
        s.session_id = sid
        s.draft_channel_id = chan
        s.lobby_ready_check_message_id = msg
        return s

    def _db(self, stale_list):
        session = MagicMock()
        select_result = MagicMock()
        select_result.scalars.return_value.all.return_value = stale_list
        session.execute = AsyncMock(return_value=select_result)
        begin = MagicMock()
        begin.__aenter__ = AsyncMock(return_value=session)
        begin.__aexit__ = AsyncMock(return_value=None)
        session.begin = MagicMock(return_value=begin)
        outer = MagicMock()
        outer.__aenter__ = AsyncMock(return_value=session)
        outer.__aexit__ = AsyncMock(return_value=None)
        return MagicMock(return_value=outer), session

    async def test_strips_buttons_and_clears_column(self):
        factory, session = self._db([self._stale()])
        msg = MagicMock()
        msg.edit = AsyncMock()
        channel = MagicMock()
        channel.fetch_message = AsyncMock(return_value=msg)
        bot = MagicMock()
        bot.get_channel.return_value = channel
        with patch("utils.AsyncSessionLocal", factory):
            await utils.strip_stale_lobby_ready_checks(bot)

        channel.fetch_message.assert_awaited_once_with(555)
        msg.edit.assert_awaited_once_with(view=None)
        assert session.execute.await_count >= 2  # SELECT + UPDATE(clear)

    async def test_message_not_found_still_clears_column(self):
        factory, session = self._db([self._stale()])
        channel = MagicMock()
        channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))
        bot = MagicMock()
        bot.get_channel.return_value = channel
        with patch("utils.AsyncSessionLocal", factory):
            await utils.strip_stale_lobby_ready_checks(bot)  # must not raise

        assert session.execute.await_count >= 2  # clear still ran

    async def test_no_stale_sessions_is_noop(self):
        factory, session = self._db([])
        bot = MagicMock()
        with patch("utils.AsyncSessionLocal", factory):
            await utils.strip_stale_lobby_ready_checks(bot)

        bot.get_channel.assert_not_called()


# ---------------------------------------------------------------------------
# Timeout / debounce ordering invariant (D)
# ---------------------------------------------------------------------------

def test_timeout_exceeds_debounce():
    """The stall timeout must outlast the debounce so a host can re-fire (which
    cleans up silently) before the timeout would fire and post a notice."""
    from ready_check import READY_CHECK_TIMEOUT_SECONDS
    assert READY_CHECK_TIMEOUT_SECONDS > READY_CHECK_DEBOUNCE_SECONDS
