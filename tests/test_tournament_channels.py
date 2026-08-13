"""Fixed homes for tournament messages: a standings channel the bot owns and a
play channel it merely adopts, both resolved by stored id with a fallback."""
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from helpers.tournament_channels import (
    PLAY_CHANNEL_SETTING,
    STANDINGS_CHANNEL_NAME,
    STANDINGS_CHANNEL_SETTING,
    ensure_standings_channel,
    resolve_channel,
)

GUILD = 1355718878298116096


def guild_stub(channels=(), categories=(), created=None):
    """A discord.Guild stand-in: get_channel by id, categories by name."""
    by_id = {c.id: c for c in channels}
    guild = MagicMock()
    guild.id = GUILD
    guild.get_channel.side_effect = by_id.get
    guild.text_channels = list(channels)
    guild.categories = list(categories)
    guild.me = MagicMock()
    guild.default_role = MagicMock()
    guild.create_text_channel = AsyncMock(return_value=created)
    return guild


def channel_stub(channel_id, name):
    channel = MagicMock()
    channel.id = channel_id
    channel.name = name
    channel.mention = f"#{name}"
    channel.send = AsyncMock()
    channel.set_permissions = AsyncMock()
    channel.overwrites_for.return_value = MagicMock(send_messages=True, embed_links=True)
    return channel


class TestResolveChannel:
    def test_none_when_nothing_configured(self):
        guild = guild_stub()
        assert resolve_channel(guild, {}, STANDINGS_CHANNEL_SETTING) is None

    def test_none_when_the_stored_channel_is_gone(self):
        """A deleted channel must not resurrect as a stale id — callers fall back."""
        guild = guild_stub(channels=[])
        config = {"tournament": {PLAY_CHANNEL_SETTING: "404"}}
        assert resolve_channel(guild, config, PLAY_CHANNEL_SETTING) is None

    def test_renaming_the_channel_does_not_lose_it(self):
        """Ids are stored precisely so an admin rename is a non-event."""
        renamed = channel_stub(111, "league-standings-archive")
        guild = guild_stub(channels=[renamed])
        config = {"tournament": {STANDINGS_CHANNEL_SETTING: "111"}}
        assert resolve_channel(guild, config, STANDINGS_CHANNEL_SETTING) is renamed


class TestEnsureStandingsChannel:
    @pytest.mark.asyncio
    async def test_adopts_an_explicitly_named_channel_without_creating(self):
        chosen = channel_stub(222, "league-standings")
        guild = guild_stub(channels=[chosen])
        channel, created = await ensure_standings_channel(guild, {}, chosen)
        assert (channel, created) == (chosen, False)
        guild.create_text_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_reuses_the_configured_channel(self):
        existing = channel_stub(111, STANDINGS_CHANNEL_NAME)
        guild = guild_stub(channels=[existing])
        config = {"tournament": {STANDINGS_CHANNEL_SETTING: "111"}}
        channel, created = await ensure_standings_channel(guild, config, None)
        assert (channel, created) == (existing, False)
        guild.create_text_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_adopts_a_same_named_channel_before_creating_a_duplicate(self):
        existing = channel_stub(111, STANDINGS_CHANNEL_NAME)
        guild = guild_stub(channels=[existing])
        channel, created = await ensure_standings_channel(guild, {}, None)
        assert (channel, created) == (existing, False)
        guild.create_text_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_a_read_only_channel_when_there_is_none(self):
        made = channel_stub(333, STANDINGS_CHANNEL_NAME)
        guild = guild_stub(created=made)
        channel, created = await ensure_standings_channel(guild, {}, None)
        assert (channel, created) == (made, True)
        kwargs = guild.create_text_channel.call_args.kwargs
        assert kwargs["name"] == STANDINGS_CHANNEL_NAME
        # Players read standings; only the bot writes them.
        overwrites = kwargs["overwrites"]
        assert overwrites[guild.default_role].send_messages is False
        assert overwrites[guild.default_role].read_messages is True
        assert overwrites[guild.me].send_messages is True
        assert overwrites[guild.me].embed_links is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("categories", [
        # The shape every real configs/*.json and config.py's defaults use...
        {"draft": "Draft Channels"},
        # ...and the one the /setup wizard writes (bool flag + separate name).
        {"draft": True, "draft_name": "Draft Channels"},
    ])
    async def test_new_channel_lands_in_the_configured_draft_category(self, categories):
        category = MagicMock()
        category.name = "Draft Channels"
        made = channel_stub(333, STANDINGS_CHANNEL_NAME)
        guild = guild_stub(categories=[category], created=made)
        await ensure_standings_channel(guild, {"categories": categories}, None)
        assert guild.create_text_channel.call_args.kwargs["category"] is category

    @pytest.mark.asyncio
    async def test_no_category_configured_creates_at_the_top_level(self):
        made = channel_stub(333, STANDINGS_CHANNEL_NAME)
        guild = guild_stub(created=made)
        await ensure_standings_channel(guild, {}, None)
        assert guild.create_text_channel.call_args.kwargs["category"] is None

    @pytest.mark.asyncio
    async def test_repairs_the_bots_write_permission_on_an_existing_channel(self):
        """A standings channel the bot can't post in is worse than none."""
        existing = channel_stub(111, STANDINGS_CHANNEL_NAME)
        existing.overwrites_for.return_value = MagicMock(send_messages=False, embed_links=False)
        guild = guild_stub(channels=[existing])
        config = {"tournament": {STANDINGS_CHANNEL_SETTING: "111"}}
        await ensure_standings_channel(guild, config, None)
        existing.set_permissions.assert_awaited_once()
        assert existing.set_permissions.await_args.kwargs["send_messages"] is True


class TestCogWiring:
    """The cog resolves a destination per message kind, falling back to the
    channel the command was typed in so unconfigured guilds are unaffected."""

    def _cog(self):
        from cogs.tournament_commands import TournamentCog
        return TournamentCog(MagicMock())

    def _ctx(self, guild, here):
        ctx = MagicMock()
        ctx.guild = guild
        ctx.channel = here
        return ctx

    def test_falls_back_to_the_invoking_channel(self, monkeypatch):
        import cogs.tournament_commands as mod
        here = channel_stub(999, "general")
        monkeypatch.setattr(mod, "get_config", lambda _gid: {})
        cog = self._cog()
        assert cog._destination(self._ctx(guild_stub(), here), STANDINGS_CHANNEL_SETTING) is here

    def test_uses_the_configured_channel_when_set(self, monkeypatch):
        import cogs.tournament_commands as mod
        here = channel_stub(999, "general")
        configured = channel_stub(111, STANDINGS_CHANNEL_NAME)
        guild = guild_stub(channels=[configured])
        monkeypatch.setattr(
            mod, "get_config",
            lambda _gid: {"tournament": {STANDINGS_CHANNEL_SETTING: "111"}})
        cog = self._cog()
        assert cog._destination(self._ctx(guild, here), STANDINGS_CHANNEL_SETTING) is configured

    def test_standings_and_pairings_resolve_independently(self, monkeypatch):
        import cogs.tournament_commands as mod
        here = channel_stub(999, "general")
        standings = channel_stub(111, STANDINGS_CHANNEL_NAME)
        play = channel_stub(222, "tournament-play")
        guild = guild_stub(channels=[standings, play])
        monkeypatch.setattr(mod, "get_config", lambda _gid: {"tournament": {
            STANDINGS_CHANNEL_SETTING: "111", PLAY_CHANNEL_SETTING: "222"}})
        cog, ctx = self._cog(), self._ctx(guild, here)
        assert cog._destination(ctx, STANDINGS_CHANNEL_SETTING) is standings
        assert cog._destination(ctx, PLAY_CHANNEL_SETTING) is play

    async def _post_standings(self, tournament_id, message_id, pin_error=None):
        """Run _post_standings against a seeded tournament; return the message."""
        from database.db_session import AsyncSessionLocal
        from models.tournament import Tournament

        async with AsyncSessionLocal() as session:
            session.add(Tournament(id=tournament_id, guild_id=str(GUILD), name="T", total_rounds=4))
            await session.commit()

        message = MagicMock()
        message.id = message_id
        message.channel.id = 111
        message.pin = AsyncMock(side_effect=pin_error)
        channel = channel_stub(111, STANDINGS_CHANNEL_NAME)
        channel.send = AsyncMock(return_value=message)

        await self._cog()._post_standings(channel, tournament_id=tournament_id)
        return message

    async def _saved(self, tournament_id):
        from database.db_session import AsyncSessionLocal
        from models.tournament import Tournament

        async with AsyncSessionLocal() as session:
            return await session.get(Tournament, tournament_id)

    @pytest.mark.asyncio
    async def test_standings_message_is_pinned_and_its_home_recorded(self, test_db):
        """Pinning is what keeps an edited-in-place message findable, and the
        recorded channel/message is what later in-place edits steer by."""
        message = await self._post_standings(tournament_id=1, message_id=7)

        message.pin.assert_awaited_once()
        saved = await self._saved(1)
        assert (saved.standings_channel_id, saved.standings_message_id) == ("111", "7")

    @pytest.mark.asyncio
    async def test_a_failed_pin_does_not_lose_the_standings(self, test_db):
        """Pin limits are a Discord fact of life; the standings still count."""
        await self._post_standings(
            tournament_id=2, message_id=8,
            pin_error=discord.errors.HTTPException(MagicMock(), "pin limit"))

        assert (await self._saved(2)).standings_message_id == "8"
