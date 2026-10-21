"""The victory message links to the draft's public log once it is posted.

The publish saves the log message's ids, then re-renders the victory messages
to add the link, and only after that marks the draft data_received. Gating the
link on data_received meant that re-render never added it.
"""
from types import SimpleNamespace

import discord

from utils import add_draft_log_link


def _draft():
    return SimpleNamespace(guild_id="1", data_received=False,
                           logs_channel_id=None, logs_message_id=None)


def test_the_link_is_added_as_soon_as_the_log_message_exists():
    embed = discord.Embed()
    draft = _draft()
    draft.logs_channel_id, draft.logs_message_id = "22", "333"   # data_received still False

    add_draft_log_link(embed, draft)

    assert [(f.name, f.value) for f in embed.fields] == [
        ("Draft Logs", "[View Draft Log](https://discord.com/channels/1/22/333)")]


def test_no_link_before_the_log_is_posted():
    embed = discord.Embed()

    add_draft_log_link(embed, _draft())

    assert embed.fields == []
