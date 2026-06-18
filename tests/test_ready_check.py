"""Unit tests for ready_check.py.

Covers pure logic and all classmethod paths using mocks — no live Discord needed.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from ready_check import (
    ReadyCheckCancelConfirmView,
    ReadyCheckSession,
)


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
    def _make_channel(self, draft_msg=None):
        channel = MagicMock()
        channel.send = AsyncMock()
        msg = draft_msg or MagicMock()
        msg.edit = AsyncMock()
        channel.fetch_message = AsyncMock(return_value=msg)
        return channel

    def _make_session(self, message_id="777", channel_id="888"):
        s = MagicMock()
        s.message_id = message_id
        s.draft_channel_id = channel_id
        s.session_type = "random"
        s.team_a_name = "Team A"
        s.team_b_name = "Team B"
        return s

    async def test_posts_cancellation_message(self):
        channel = self._make_channel()
        bot = MagicMock()
        with patch("ready_check.get_draft_session", AsyncMock(return_value=self._make_session())), \
             patch.object(ReadyCheckSession, "cleanup", AsyncMock()), \
             patch("ready_check.state_manager"), \
             patch("views.PersistentView", MagicMock()):
            await ReadyCheckSession.cancel("sid", bot, channel, cancelled_by="Alice")

        channel.send.assert_awaited_once()
        assert "Alice" in channel.send.call_args.args[0]

    async def test_restores_draft_message_view(self):
        channel = self._make_channel()
        bot = MagicMock()
        session = self._make_session()
        mock_persistent_view = MagicMock()

        with patch("ready_check.get_draft_session", AsyncMock(return_value=session)), \
             patch.object(ReadyCheckSession, "cleanup", AsyncMock()), \
             patch("ready_check.state_manager"), \
             patch("views.PersistentView", return_value=mock_persistent_view):
            await ReadyCheckSession.cancel("sid", bot, channel, cancelled_by="Bob")

        channel.fetch_message.assert_awaited_once_with(int(session.message_id))
        channel.fetch_message.return_value.edit.assert_awaited_once()

    async def test_no_session_skips_draft_message_edit(self):
        channel = self._make_channel()
        bot = MagicMock()
        with patch("ready_check.get_draft_session", AsyncMock(return_value=None)), \
             patch.object(ReadyCheckSession, "cleanup", AsyncMock()), \
             patch("ready_check.state_manager"):
            await ReadyCheckSession.cancel("sid", bot, channel, cancelled_by="Eve")

        channel.fetch_message.assert_not_awaited()

    async def test_draft_message_not_found_is_swallowed(self):
        channel = self._make_channel()
        channel.fetch_message = AsyncMock(
            side_effect=discord.NotFound(MagicMock(), "gone")
        )
        bot = MagicMock()
        with patch("ready_check.get_draft_session", AsyncMock(return_value=self._make_session())), \
             patch.object(ReadyCheckSession, "cleanup", AsyncMock()), \
             patch("ready_check.state_manager"), \
             patch("views.PersistentView", MagicMock()):
            # Should not raise
            await ReadyCheckSession.cancel("sid", bot, channel, cancelled_by="Eve")


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
