"""Leaderboards post in clusters: every category belongs to exactly one group,
and the group order is what decides the channel layout."""
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from leaderboard_config import (
    ALL_CATEGORIES,
    CATEGORY_CONFIGS,
    LEADERBOARD_GROUPS,
    STREAK_CATEGORIES,
)


def _grouped_categories():
    return [c for group in LEADERBOARD_GROUPS for c in group["categories"]]


class TestGroupsCoverEveryCategory:
    def test_every_category_is_in_exactly_one_group(self):
        """An ungrouped category would silently vanish from the channel, since
        the cog iterates groups rather than CATEGORY_CONFIGS."""
        grouped = _grouped_categories()
        assert sorted(grouped) == sorted(CATEGORY_CONFIGS)
        assert len(grouped) == len(set(grouped)), "a category appears in two groups"

    def test_all_categories_follows_group_order(self):
        assert ALL_CATEGORIES == _grouped_categories()

    def test_groups_have_the_fields_the_header_renders(self):
        for group in LEADERBOARD_GROUPS:
            assert group["key"] and group["title"] and group["blurb"]
        keys = [g["key"] for g in LEADERBOARD_GROUPS]
        assert len(keys) == len(set(keys)), "duplicate group key"

    def test_streaks_are_grouped_together(self):
        """The streak boards share a timeframe vocabulary (active/30d/90d/
        lifetime), so they should read as one cluster."""
        streaks = next(g for g in LEADERBOARD_GROUPS if g["key"] == "streaks")
        assert set(STREAK_CATEGORIES) <= set(streaks["categories"])


def _record(**overrides):
    record = MagicMock()
    record.group_header_message_ids = {}
    record.message_id = "1"
    for category in ALL_CATEGORIES:
        setattr(record, f"{category}_view_message_id", None)
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


def _channel(sent_id=999):
    channel = MagicMock()
    message = MagicMock()
    message.id = sent_id
    message.edit = AsyncMock()
    message.delete = AsyncMock()
    channel.send = AsyncMock(return_value=message)
    channel.fetch_message = AsyncMock(return_value=message)
    return channel, message


def _cog():
    from cogs.leaderboard import LeaderboardCog
    return LeaderboardCog(MagicMock())


class TestGroupHeaders:
    @pytest.mark.asyncio
    async def test_posts_a_header_and_remembers_it(self, monkeypatch):
        import cogs.leaderboard as mod
        monkeypatch.setattr(mod, "db_session", _fake_session)
        channel, message = _channel(sent_id=555)
        record = _record()
        group = LEADERBOARD_GROUPS[0]

        await _cog()._update_group_header(channel, record, group)

        content = channel.send.await_args.args[0]
        assert group["title"] in content and group["blurb"] in content
        assert record.group_header_message_ids[group["key"]] == "555"

    @pytest.mark.asyncio
    async def test_edits_the_existing_header_instead_of_reposting(self, monkeypatch):
        import cogs.leaderboard as mod
        monkeypatch.setattr(mod, "db_session", _fake_session)
        channel, message = _channel()
        group = LEADERBOARD_GROUPS[0]
        record = _record(group_header_message_ids={group["key"]: "42"})

        await _cog()._update_group_header(channel, record, group)

        message.edit.assert_awaited_once()
        channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_reposts_when_the_header_was_deleted(self, monkeypatch):
        import cogs.leaderboard as mod
        monkeypatch.setattr(mod, "db_session", _fake_session)
        channel, message = _channel(sent_id=777)
        channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))
        group = LEADERBOARD_GROUPS[0]
        record = _record(group_header_message_ids={group["key"]: "42"})

        await _cog()._update_group_header(channel, record, group)

        channel.send.assert_awaited_once()
        assert record.group_header_message_ids[group["key"]] == "777"


class TestRebuild:
    @pytest.mark.asyncio
    async def test_rebuild_deletes_headers_and_boards(self, monkeypatch):
        """Ordering in Discord is post order, so a rebuild has to remove the
        old messages before the grouped order can take effect."""
        import cogs.leaderboard as mod
        monkeypatch.setattr(mod, "db_session", _fake_session)
        channel, message = _channel()
        record = _record(group_header_message_ids={"performance": "10"})
        record.draft_record_view_message_id = "11"

        await _cog()._clear_posted_messages(channel, record)

        assert message.delete.await_count >= 2   # header + board (+ hot_streak)

    def test_tracked_ids_include_headers_boards_and_hot_streak(self):
        record = _record(group_header_message_ids={"performance": "10"}, message_id="99")
        record.match_win_view_message_id = "11"
        ids = _cog()._tracked_message_ids(record)
        assert set(ids) == {"10", "11", "99"}

    def test_tracked_ids_skip_unposted_boards(self):
        """None entries would become fetch_message(None) crashes."""
        record = _record(message_id="")
        assert _cog()._tracked_message_ids(record) == []


class _FakeSession:
    """Stands in for db_session(): merge returns the record unchanged."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def merge(self, record):
        return record

    async def commit(self):
        return None


def _fake_session():
    return _FakeSession()
