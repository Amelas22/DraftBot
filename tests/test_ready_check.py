"""Unit tests for ready_check.py.

Covers pure logic and all classmethod paths using mocks — no live Discord needed.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from ready_check import (
    ReadyCheckCancelConfirmView,
    ReadyCheckSession,
    ReadyCheckView,
)


# ---------------------------------------------------------------------------
# counts
# ---------------------------------------------------------------------------

class TestCounts:
    def test_counts_per_bucket(self):
        rc = ReadyCheckSession(player_ids=["1", "2", "3", "4", "5"])
        rc.set_status("1", "ready")
        rc.set_status("2", "ready")
        rc.set_status("3", "not_ready")
        assert rc.counts() == {"ready": 2, "not_ready": 1, "no_response": 2}

    def test_counts_empty(self):
        assert ReadyCheckSession(player_ids=[]).counts() == {"ready": 0, "not_ready": 0, "no_response": 0}


# ---------------------------------------------------------------------------
# _all_ready
# ---------------------------------------------------------------------------

class TestReadyCheckSessionAllReady:
    def _rc(self, ready=(), not_ready=(), no_response=()):
        rc = ReadyCheckSession(player_ids=list(ready) + list(not_ready) + list(no_response))
        for pid in ready:
            rc.set_status(pid, 'ready')
        for pid in not_ready:
            rc.set_status(pid, 'not_ready')
        return rc

    def test_all_responded_ready(self):
        assert self._rc(ready=["1", "2", "3"]).all_ready() is True

    def test_some_not_ready(self):
        assert self._rc(ready=["1"], not_ready=["2"]).all_ready() is False

    def test_some_no_response(self):
        assert self._rc(ready=["1"], no_response=["2"]).all_ready() is False

    def test_nobody_ready(self):
        assert self._rc(no_response=["1", "2"]).all_ready() is False

    def test_no_players(self):
        assert ReadyCheckSession(player_ids=[]).all_ready() is False

    def test_single_player_ready(self):
        assert self._rc(ready=["1"]).all_ready() is True

    def test_mixed_all_three_lists(self):
        assert self._rc(ready=["1"], not_ready=["2"], no_response=["3"]).all_ready() is False


# ---------------------------------------------------------------------------
# ReadyCheckSession.build_embed
# ---------------------------------------------------------------------------

def _make_rc_with_statuses(ready=(), not_ready=(), no_response=(), message_id=None):
    """Build a ReadyCheckSession with explicit per-bucket membership."""
    rc = ReadyCheckSession(player_ids=list(ready) + list(not_ready) + list(no_response), message_id=message_id)
    for pid in ready:
        rc.set_status(pid, 'ready')
    for pid in not_ready:
        rc.set_status(pid, 'not_ready')
    return rc


@pytest.mark.asyncio
class TestBuildEmbed:
    async def test_returns_embed_with_three_fields(self):
        sign_ups = {"1": "Alice", "2": "Bob"}
        rc = _make_rc_with_statuses(ready=["1"], no_response=["2"])
        embed = await rc.build_embed(sign_ups)
        assert isinstance(embed, discord.Embed)
        field_names = [f.name for f in embed.fields]
        assert "Ready" in field_names
        assert "Not Ready" in field_names
        assert "No Response" in field_names

    async def test_none_shown_for_empty_list(self):
        sign_ups = {"1": "Alice"}
        rc = _make_rc_with_statuses(ready=["1"])
        embed = await rc.build_embed(sign_ups)
        not_ready_field = next(f for f in embed.fields if f.name == "Not Ready")
        assert not_ready_field.value == "None"

    async def test_falls_back_to_unknown_user(self):
        sign_ups = {}
        rc = _make_rc_with_statuses(ready=["999"])
        embed = await rc.build_embed(sign_ups)
        ready_field = next(f for f in embed.fields if f.name == "Ready")
        assert "Unknown user" in ready_field.value


# ---------------------------------------------------------------------------
# ReadyCheckSession.cleanup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCleanup:
    def _make_channel(self, raise_not_found=False, raise_other=False):
        channel = MagicMock()
        if raise_not_found:
            channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(), "not found"))
        elif raise_other:
            channel.fetch_message = AsyncMock(side_effect=RuntimeError("boom"))
        else:
            msg = MagicMock()
            msg.delete = AsyncMock()
            channel.fetch_message = AsyncMock(return_value=msg)
        return channel

    def _rc(self, message_id=42):
        return ReadyCheckSession(player_ids=["1"], message_id=message_id)

    async def test_deletes_message_and_clears_state(self):
        channel = self._make_channel()
        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = self._rc(message_id=42)
            await ReadyCheckSession.cleanup("sid", channel)

        channel.fetch_message.assert_awaited_once_with(42)
        channel.fetch_message.return_value.delete.assert_awaited_once()
        sm.remove_ready_check.assert_called_once_with("sid")

    async def test_no_message_id_skips_fetch(self):
        channel = self._make_channel()
        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = self._rc(message_id=None)
            await ReadyCheckSession.cleanup("sid", channel)

        channel.fetch_message.assert_not_awaited()
        sm.remove_ready_check.assert_called_once_with("sid")

    async def test_no_ready_check_skips_fetch(self):
        channel = self._make_channel()
        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = None
            await ReadyCheckSession.cleanup("sid", channel)

        channel.fetch_message.assert_not_awaited()
        sm.remove_ready_check.assert_called_once_with("sid")

    async def test_none_channel_skips_fetch(self):
        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = self._rc(message_id=99)
            await ReadyCheckSession.cleanup("sid", None)

        sm.remove_ready_check.assert_called_once_with("sid")

    async def test_not_found_is_swallowed(self):
        channel = self._make_channel(raise_not_found=True)
        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = self._rc()
            await ReadyCheckSession.cleanup("sid", channel)  # Should not raise

        sm.remove_ready_check.assert_called_once_with("sid")

    async def test_other_error_is_swallowed_state_still_cleared(self):
        channel = self._make_channel(raise_other=True)
        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = self._rc()
            await ReadyCheckSession.cleanup("sid", channel)

        sm.remove_ready_check.assert_called_once_with("sid")


# ---------------------------------------------------------------------------
# ReadyCheckSession.cancel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCancel:
    def _make_channel(self):
        channel = MagicMock()
        channel.send = AsyncMock()
        return channel

    async def test_posts_cancellation_message(self):
        channel = self._make_channel()
        with patch.object(ReadyCheckSession, "cleanup", AsyncMock()), \
             patch("ready_check.state_manager"):
            await ReadyCheckSession.cancel("sid", channel, cancelled_by="Alice")

        channel.send.assert_awaited_once()
        assert "Alice" in channel.send.call_args.args[0]

    async def test_delegates_to_cleanup(self):
        channel = self._make_channel()
        with patch.object(ReadyCheckSession, "cleanup", AsyncMock()) as mock_cleanup, \
             patch("ready_check.state_manager"):
            await ReadyCheckSession.cancel("sid", channel, cancelled_by="Bob")

        mock_cleanup.assert_awaited_once_with("sid", channel)


# ---------------------------------------------------------------------------
# ReadyCheckSession.sync_removed_player
# ---------------------------------------------------------------------------

def _make_rc(ready=(), not_ready=(), no_response=(), message_id=55):
    return _make_rc_with_statuses(ready=ready, not_ready=not_ready, no_response=no_response, message_id=message_id)


def _make_interaction(guild=None):
    interaction = MagicMock()
    interaction.guild = guild or MagicMock()
    msg = MagicMock()
    msg.edit = AsyncMock()
    interaction.channel = MagicMock()
    interaction.channel.fetch_message = AsyncMock(return_value=msg)
    return interaction


def _make_draft_session():
    ds = MagicMock()
    ds.sign_ups = {"1": "Alice", "2": "Bob"}
    ds.draft_link = None
    return ds


@pytest.mark.asyncio
class TestSyncRemovedPlayer:
    async def test_removes_player_from_ready(self):
        rc = _make_rc(ready=["1", "2"])
        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = rc
            await ReadyCheckSession.sync_removed_player("sid", "1", _make_draft_session(), _make_interaction())

        assert "1" not in rc.ready
        assert "2" in rc.ready

    async def test_removes_player_from_no_response(self):
        rc = _make_rc(ready=["1"], no_response=["2"])
        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = rc
            await ReadyCheckSession.sync_removed_player("sid", "2", _make_draft_session(), _make_interaction())

        assert "2" not in rc.no_response

    async def test_no_active_ready_check_is_noop(self):
        interaction = _make_interaction()
        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = None
            await ReadyCheckSession.sync_removed_player("sid", "1", _make_draft_session(), interaction)

        interaction.channel.fetch_message.assert_not_awaited()

    async def test_no_message_id_is_noop(self):
        rc = _make_rc(no_response=["1"], message_id=None)
        interaction = _make_interaction()
        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = rc
            await ReadyCheckSession.sync_removed_player("sid", "1", _make_draft_session(), interaction)

        interaction.channel.fetch_message.assert_not_awaited()

    async def test_message_not_found_clears_message_id(self):
        rc = _make_rc(ready=["1"], no_response=["2"])
        interaction = _make_interaction()
        interaction.channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))

        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = rc
            await ReadyCheckSession.sync_removed_player("sid", "2", _make_draft_session(), interaction)

        assert rc.message_id is None

    async def test_triggers_auto_create_when_all_ready(self):
        rc = _make_rc(ready=["1"], no_response=["2"])
        with patch("ready_check.state_manager") as sm, \
             patch.object(ReadyCheckSession, "handle_all_ready", AsyncMock()) as mock_trigger:
            sm.get_ready_check.return_value = rc
            await ReadyCheckSession.sync_removed_player("sid", "2", _make_draft_session(), _make_interaction())

        mock_trigger.assert_awaited_once()

    async def test_does_not_handle_all_ready_when_not_all_ready(self):
        rc = _make_rc(ready=["1"], no_response=["2", "3"])
        with patch("ready_check.state_manager") as sm, \
             patch.object(ReadyCheckSession, "handle_all_ready", AsyncMock()) as mock_trigger:
            sm.get_ready_check.return_value = rc
            await ReadyCheckSession.sync_removed_player("sid", "2", _make_draft_session(), _make_interaction())

        mock_trigger.assert_not_awaited()


# ---------------------------------------------------------------------------
# ReadyCheckSession.sync_added_player
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSyncAddedPlayer:
    async def test_adds_player_to_no_response(self):
        rc = _make_rc(ready=["1"])
        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = rc
            await ReadyCheckSession.sync_added_player("sid", "2", _make_draft_session(), _make_interaction())

        assert "2" in rc.no_response

    async def test_no_active_ready_check_is_noop(self):
        interaction = _make_interaction()
        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = None
            await ReadyCheckSession.sync_added_player("sid", "2", _make_draft_session(), interaction)

        interaction.channel.fetch_message.assert_not_awaited()

    async def test_already_tracked_is_noop(self):
        rc = _make_rc(ready=["1"], no_response=["2"])
        interaction = _make_interaction()
        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = rc
            await ReadyCheckSession.sync_added_player("sid", "2", _make_draft_session(), interaction)

        assert rc.no_response.count("2") == 1  # Not added a second time

    async def test_no_message_id_skips_embed_update(self):
        rc = _make_rc(ready=["1"], message_id=None)
        interaction = _make_interaction()
        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = rc
            await ReadyCheckSession.sync_added_player("sid", "2", _make_draft_session(), interaction)

        interaction.channel.fetch_message.assert_not_awaited()
        assert "2" in rc.no_response  # Player was still added to state

    async def test_message_not_found_clears_message_id(self):
        rc = _make_rc(ready=["1"])
        interaction = _make_interaction()
        interaction.channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))

        with patch("ready_check.state_manager") as sm:
            sm.get_ready_check.return_value = rc
            await ReadyCheckSession.sync_added_player("sid", "2", _make_draft_session(), interaction)

        assert rc.message_id is None


# ---------------------------------------------------------------------------
# ReadyCheckSession.run_timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRunTimeout:
    def _make_channel(self):
        channel = MagicMock()
        channel.send = AsyncMock()
        msg = MagicMock()
        msg.delete = AsyncMock()
        channel.fetch_message = AsyncMock(return_value=msg)
        return channel

    def _guild(self, gid="42"):
        guild = MagicMock()
        guild.id = gid
        return guild

    async def test_superseded_is_noop(self):
        """A re-fire replaced this check -> timeout does nothing (the re-fire cleaned up)."""
        rc = _make_rc(ready=["1"], no_response=["2"])
        channel = self._make_channel()
        with patch("ready_check.asyncio.sleep", AsyncMock()), \
             patch("ready_check.state_manager") as sm, \
             patch("ready_check.SignUpHistory") as sh:
            sm.get_ready_check.return_value = object()  # not `rc`
            await rc.run_timeout("sid", channel, self._guild())

        sh.record_ready_event.assert_not_called()
        channel.send.assert_not_awaited()
        sm.remove_ready_check.assert_not_called()

    async def test_all_ready_is_noop(self):
        rc = _make_rc(ready=["1", "2"])
        channel = self._make_channel()
        with patch("ready_check.asyncio.sleep", AsyncMock()), \
             patch("ready_check.state_manager") as sm, \
             patch("ready_check.SignUpHistory") as sh:
            sm.get_ready_check.return_value = rc
            await rc.run_timeout("sid", channel, self._guild())

        sh.record_ready_event.assert_not_called()
        channel.send.assert_not_awaited()

    async def test_no_non_responders_is_noop(self):
        """Everyone responded (some Not Ready) -> not a silent stall, no notice."""
        rc = _make_rc(ready=["1"], not_ready=["2"])
        channel = self._make_channel()
        with patch("ready_check.asyncio.sleep", AsyncMock()), \
             patch("ready_check.state_manager") as sm, \
             patch("ready_check.SignUpHistory") as sh:
            sm.get_ready_check.return_value = rc
            await rc.run_timeout("sid", channel, self._guild())

        sh.record_ready_event.assert_not_called()
        channel.send.assert_not_awaited()

    async def test_genuine_stall_audits_cleans_and_notifies(self):
        rc = _make_rc(ready=["1"], no_response=["2", "3"], message_id=55)
        channel = self._make_channel()
        ds = MagicMock()
        ds.sign_ups = {"1": "Alice", "2": "Bob", "3": "Carol"}
        with patch("ready_check.asyncio.sleep", AsyncMock()), \
             patch("ready_check.state_manager") as sm, \
             patch("ready_check.get_draft_session", AsyncMock(return_value=ds)), \
             patch("ready_check.SignUpHistory") as sh:
            sm.get_ready_check.return_value = rc
            sh.record_ready_event = AsyncMock()
            await rc.run_timeout("sid", channel, self._guild())

        # One audit row per non-responder, all tagged ready_timeout.
        assert sh.record_ready_event.await_count == 2
        for call in sh.record_ready_event.await_args_list:
            assert call.kwargs["action"] == "ready_timeout"
        # Stale message deleted, state dropped, notice posted naming the non-responders.
        channel.fetch_message.assert_awaited_once_with(55)
        channel.fetch_message.return_value.delete.assert_awaited_once()
        sm.remove_ready_check.assert_called_once_with("sid")
        channel.send.assert_awaited_once()
        notice = channel.send.call_args.args[0]
        assert "Bob" in notice and "Carol" in notice


# ---------------------------------------------------------------------------
# ReadyCheckView._handle_status (the click handler)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHandleStatus:
    def _interaction(self, user_id="1", guild_id="42"):
        i = MagicMock()
        i.user.id = user_id
        i.guild.id = guild_id
        i.response.send_message = AsyncMock()
        i.response.edit_message = AsyncMock()
        return i

    async def test_records_audit_and_updates_embed(self):
        rc = _make_rc(no_response=["1", "2"])  # readying "1" leaves "2" pending
        view = ReadyCheckView("sid")
        i = self._interaction(user_id="1")
        with patch("ready_check.state_manager") as sm, \
             patch("ready_check.get_draft_session", AsyncMock(return_value=_make_draft_session())), \
             patch("ready_check.SignUpHistory") as sh, \
             patch.object(ReadyCheckSession, "build_embed", AsyncMock(return_value=MagicMock())):
            sm.get_ready_check.return_value = rc
            sh.record_ready_event = AsyncMock()
            await view._handle_status(i, "ready")

        assert "1" in rc.ready
        sh.record_ready_event.assert_awaited_once()
        assert sh.record_ready_event.await_args.kwargs["action"] == "ready"
        i.response.edit_message.assert_awaited_once()

    async def test_missing_session_warns_and_skips(self):
        view = ReadyCheckView("sid")
        i = self._interaction()
        with patch("ready_check.state_manager") as sm, \
             patch("ready_check.SignUpHistory") as sh:
            sm.get_ready_check.return_value = None
            await view._handle_status(i, "ready")

        i.response.send_message.assert_awaited_once()
        assert i.response.send_message.call_args.kwargs.get("ephemeral") is True
        sh.record_ready_event.assert_not_called()
        i.response.edit_message.assert_not_awaited()

    async def test_unauthorized_user_no_state_change(self):
        rc = _make_rc(no_response=["1"])  # only player "1" is in the check
        view = ReadyCheckView("sid")
        i = self._interaction(user_id="999")  # not a participant
        with patch("ready_check.state_manager") as sm, \
             patch("ready_check.SignUpHistory") as sh:
            sm.get_ready_check.return_value = rc
            await view._handle_status(i, "ready")

        i.response.send_message.assert_awaited_once()
        sh.record_ready_event.assert_not_called()
        assert rc.no_response == ["1"]  # unchanged

    async def test_last_ready_triggers_handle_all_ready(self):
        rc = _make_rc(no_response=["1"])  # single player; readying completes the check
        view = ReadyCheckView("sid")
        with patch("ready_check.state_manager") as sm, \
             patch("ready_check.get_draft_session", AsyncMock(return_value=_make_draft_session())), \
             patch("ready_check.SignUpHistory") as sh, \
             patch.object(ReadyCheckSession, "build_embed", AsyncMock(return_value=MagicMock())), \
             patch.object(ReadyCheckSession, "handle_all_ready", AsyncMock()) as hac:
            sm.get_ready_check.return_value = rc
            sh.record_ready_event = AsyncMock()
            await view._handle_status(self._interaction(user_id="1"), "ready")

        hac.assert_awaited_once()

    async def test_audit_failure_does_not_break_click(self):
        """A failing audit write must not abort the user's ready click."""
        rc = _make_rc(no_response=["1", "2"])
        view = ReadyCheckView("sid")
        i = self._interaction(user_id="1")
        with patch("ready_check.state_manager") as sm, \
             patch("ready_check.get_draft_session", AsyncMock(return_value=_make_draft_session())), \
             patch("ready_check.SignUpHistory") as sh, \
             patch.object(ReadyCheckSession, "build_embed", AsyncMock(return_value=MagicMock())):
            sm.get_ready_check.return_value = rc
            sh.record_ready_event = AsyncMock(side_effect=RuntimeError("db down"))
            await view._handle_status(i, "ready")  # must not raise

        assert "1" in rc.ready                       # state still applied
        i.response.edit_message.assert_awaited_once()  # click still completed


# ---------------------------------------------------------------------------
# ReadyCheckCancelConfirmView construction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestReadyCheckCancelConfirmView:
    async def test_has_two_buttons(self):
        view = ReadyCheckCancelConfirmView("sid", "Alice")
        buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
        assert len(buttons) == 2

    async def test_confirm_button_is_danger(self):
        view = ReadyCheckCancelConfirmView("sid", "Alice")
        confirm = next(c for c in view.children if "Yes" in c.label)
        assert confirm.style == discord.ButtonStyle.danger

    async def test_deny_button_is_secondary(self):
        view = ReadyCheckCancelConfirmView("sid", "Alice")
        deny = next(c for c in view.children if "No" in c.label)
        assert deny.style == discord.ButtonStyle.secondary

    async def test_stores_cancelled_by(self):
        view = ReadyCheckCancelConfirmView("sid", "Charlie")
        assert view.cancelled_by == "Charlie"

    async def test_timeout_is_60(self):
        view = ReadyCheckCancelConfirmView("sid", "Alice")
        assert view.timeout == 60
