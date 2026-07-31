"""Tests for readable draft context in debt panels: _draft_label /
describe_draft_sources / format_entry_source's draft_labels parameter.

The label replaces the unreadable "Draft #<64-char session id>" with
"[<cube> · <date>](victory-message link)"; the link targets the results
channel because it's the one draft message surviving channel cleanup.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from debt_views.helpers import _draft_label, describe_draft_sources, format_entry_source

JUL29 = datetime(2026, 7, 29, 15, 18)


# ---- _draft_label --------------------------------------------------------------------

def test_label_linked_when_victory_message_exists():
    assert _draft_label("LSVCube", JUL29, guild_id=1, channel_id=2, message_id=3) == \
        "[LSVCube · Jul 29](https://discord.com/channels/1/2/3)"


def test_label_unlinked_without_victory_message():
    assert _draft_label("LSVCube", JUL29, guild_id=1, channel_id=2, message_id=None) == \
        "LSVCube · Jul 29"


def test_label_degrades_without_cube_or_date():
    assert _draft_label(None, JUL29) == "Draft · Jul 29"
    assert _draft_label("LSVCube", None) == "LSVCube"


# ---- format_entry_source -------------------------------------------------------------

def _draft_entry(source_id="sid1"):
    return SimpleNamespace(source_type="draft", source_id=source_id)


def test_format_uses_label_when_available():
    assert format_entry_source(_draft_entry(), {"sid1": "[LSVCube · Jul 29](u)"}) == \
        "[LSVCube · Jul 29](u)"


def test_format_falls_back_to_legacy_form():
    assert format_entry_source(_draft_entry("long-id")) == "Draft #long-id"
    assert format_entry_source(_draft_entry("long-id"), {}) == "Draft #long-id"


def test_format_non_draft_types_unchanged():
    assert format_entry_source(SimpleNamespace(source_type="settlement", source_id="x")) == "Settlement"
    assert format_entry_source(SimpleNamespace(source_type="transfer", source_id="x")) == "Transfer"


# ---- describe_draft_sources ----------------------------------------------------------

def _guild_with_results_channel(channel_id=777):
    guild = MagicMock()
    guild.id = 42
    channel = MagicMock()
    channel.id = channel_id
    channel.name = "team-draft-results"
    guild.text_channels = [channel]
    return guild


def _db_returning(sessions):
    result = MagicMock()
    result.scalars.return_value.all.return_value = sessions
    db = MagicMock()

    async def execute(_q):
        return result
    db.execute = execute
    ctx = MagicMock()

    async def aenter(*a):
        return db

    async def aexit(*a):
        return None
    ctx.__aenter__ = aenter
    ctx.__aexit__ = aexit
    return MagicMock(return_value=ctx)


@pytest.mark.asyncio
async def test_labels_linked_and_unlinked_per_session():
    sessions = [
        SimpleNamespace(session_id="s1", cube="LSVCube", teams_start_time=JUL29,
                        draft_start_time=None, victory_message_id_results_channel="999"),
        SimpleNamespace(session_id="s2", cube="PowerLSV", teams_start_time=None,
                        draft_start_time=JUL29, victory_message_id_results_channel=None),
    ]
    entries = [SimpleNamespace(source_type="draft", source_id="s1"),
               SimpleNamespace(source_type="draft", source_id="s2"),
               SimpleNamespace(source_type="settlement", source_id="x")]
    with patch("debt_views.helpers.db_session", _db_returning(sessions)), \
         patch("debt_views.helpers.get_config",
               return_value={"channels": {"draft_results": "team-draft-results"}}):
        labels = await describe_draft_sources(_guild_with_results_channel(), entries)
    assert labels["s1"] == "[LSVCube · Jul 29](https://discord.com/channels/42/777/999)"
    assert labels["s2"] == "PowerLSV · Jul 29"     # no victory message -> unlinked


@pytest.mark.asyncio
async def test_no_draft_entries_short_circuits():
    entries = [SimpleNamespace(source_type="settlement", source_id="x")]
    # db_session deliberately unpatched: reaching it would blow up the test
    assert await describe_draft_sources(_guild_with_results_channel(), entries) == {}
