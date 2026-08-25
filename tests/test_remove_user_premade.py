"""Removing a player from a PREMADE draft refreshes the message and confirms.

The premade branch of UserRemovalSelect.callback called update_team_view off the
CLASS, so `interaction` bound to `self` and the real argument went missing. It
raised TypeError after the removal had already committed and before the
confirmation and ready-check sync -- the database dropped the player while the
sign-up message kept showing them, and the clicker got no reply at all.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

import views


def _session(session_type):
    return SimpleNamespace(
        session_id="s-1",
        session_type=session_type,
        sign_ups={"101": "Ana", "202": "Bo"},
        team_a=["101"], team_b=["202"],
        team_a_name="Alpha", team_b_name="Bravo",
        session_stage=None,
        draft_channel_id="900", message_id="800",
    )


def _interaction_and_message():
    """An interaction whose bot resolves to a channel holding the sign-up message,
    so the real update_team_view can fetch and edit it."""
    import discord

    embed = discord.Embed(title="Premade Draft")
    embed.add_field(name="Alpha (1):", value="Ana", inline=True)
    embed.add_field(name="Bravo (1):", value="Bo", inline=True)

    message = MagicMock()
    message.embeds = [embed]
    message.edit = AsyncMock()

    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)

    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.guild_id = 5
    interaction.client.get_channel = MagicMock(return_value=channel)
    return interaction, message


class _Db:
    """Stands in for AsyncSessionLocal(): the removal's own commit."""
    async def __aenter__(self):
        inner = MagicMock()
        inner.execute = AsyncMock()
        inner.commit = AsyncMock()
        begin = MagicMock()
        begin.__aenter__ = AsyncMock(return_value=inner)
        begin.__aexit__ = AsyncMock(return_value=False)
        inner.begin = MagicMock(return_value=begin)
        return inner

    async def __aexit__(self, *exc):
        return False


async def _run(session_type):
    select = views.UserRemovalSelect(options=[], session_id="s-1")
    session = _session(session_type)
    interaction, message = _interaction_and_message()

    # `values` is a py-cord property fed by the interaction payload; shadow it on
    # the subclass rather than reaching into the library's internals.
    with patch.object(views.UserRemovalSelect, "values", new_callable=PropertyMock,
                      return_value=["101"], create=True), \
         patch.object(views, "get_draft_session", AsyncMock(return_value=session)), \
         patch.object(views, "AsyncSessionLocal", _Db), \
         patch.object(views.SignUpHistory, "record_signup_event", AsyncMock()), \
         patch.object(views, "update_draft_message", AsyncMock()) as generic, \
         patch.object(views, "get_display_name_by_id", lambda uid, guild, name=None: name or uid), \
         patch.object(views.ReadyCheckSession, "sync_removed_player", AsyncMock()) as sync:
        # update_team_view is deliberately NOT mocked: a mock accepts the
        # interaction as `self` just as happily as as the argument, so it cannot
        # tell the bug from the fix. The real method runs against a fake channel.
        await select.callback(interaction)

    return interaction, generic, message, sync


@pytest.mark.asyncio
async def test_removing_from_a_premade_draft_refreshes_the_team_message():
    _interaction, generic, message, _sync = await _run("premade")

    # The refreshed sign-up message is the whole point: the removal used to
    # commit while this edit never happened.
    message.edit.assert_awaited_once()
    generic.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_premade_removal_still_confirms_and_syncs_the_ready_check():
    """These run AFTER the refresh, so the TypeError silently took them with it."""
    interaction, _generic, _message, sync = await _run("premade")

    interaction.followup.send.assert_awaited_once()
    assert "Ana" in str(interaction.followup.send.await_args)
    sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_random_draft_still_takes_the_other_branch():
    _interaction, generic, message, _sync = await _run("random")

    generic.assert_awaited_once()
    message.edit.assert_not_awaited()
