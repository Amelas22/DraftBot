"""Fixed homes for tournament messages: standings and pairings channels the bot
creates in the invoking category, both resolved by stored id with a fallback."""
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from helpers.tournament_channels import (
    PAIRINGS,
    STANDINGS,
    ensure_channel,
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
        assert resolve_channel(guild, {}, STANDINGS.setting) is None

    def test_none_when_the_stored_channel_is_gone(self):
        """A deleted channel must not resurrect as a stale id — callers fall back."""
        guild = guild_stub(channels=[])
        config = {"tournament": {PAIRINGS.setting: "404"}}
        assert resolve_channel(guild, config, PAIRINGS.setting) is None

    def test_renaming_the_channel_does_not_lose_it(self):
        """Ids are stored precisely so an admin rename is a non-event."""
        renamed = channel_stub(111, "league-standings-archive")
        guild = guild_stub(channels=[renamed])
        config = {"tournament": {STANDINGS.setting: "111"}}
        assert resolve_channel(guild, config, STANDINGS.setting) is renamed


def test_standings_and_pairings_specs_do_not_collide():
    """Distinct names and settings, so one kind never adopts the other's channel."""
    assert STANDINGS.name != PAIRINGS.name
    assert STANDINGS.setting != PAIRINGS.setting
    assert STANDINGS.read_only and not PAIRINGS.read_only


@pytest.mark.parametrize("spec", [STANDINGS, PAIRINGS])
def test_overwrites_are_always_a_dict(spec):
    """create_text_channel validates with isinstance(overwrites, dict) and
    rejects None, so "no overrides" has to be {} — a mocked guild would happily
    accept None and hide the InvalidArgument until a real channel was created."""
    from helpers.tournament_channels import _overwrites

    guild = guild_stub()
    overwrites = _overwrites(guild, spec)
    assert isinstance(overwrites, dict)
    assert bool(overwrites) is spec.read_only


class TestEnsureStandingsChannel:
    @pytest.mark.asyncio
    async def test_adopts_an_explicitly_named_channel_without_creating(self):
        chosen = channel_stub(222, "league-standings")
        guild = guild_stub(channels=[chosen])
        channel, created = await ensure_channel(guild, {}, STANDINGS, None, chosen)
        assert (channel, created) == (chosen, False)
        guild.create_text_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_reuses_the_configured_channel(self):
        existing = channel_stub(111, STANDINGS.name)
        guild = guild_stub(channels=[existing])
        config = {"tournament": {STANDINGS.setting: "111"}}
        channel, created = await ensure_channel(guild, config, STANDINGS, None)
        assert (channel, created) == (existing, False)
        guild.create_text_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_adopts_a_same_named_channel_before_creating_a_duplicate(self):
        existing = channel_stub(111, STANDINGS.name)
        guild = guild_stub(channels=[existing])
        channel, created = await ensure_channel(guild, {}, STANDINGS, None)
        assert (channel, created) == (existing, False)
        guild.create_text_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_a_read_only_channel_when_there_is_none(self):
        made = channel_stub(333, STANDINGS.name)
        guild = guild_stub(created=made)
        channel, created = await ensure_channel(guild, {}, STANDINGS, None)
        assert (channel, created) == (made, True)
        kwargs = guild.create_text_channel.call_args.kwargs
        assert kwargs["name"] == STANDINGS.name
        # Players read standings; only the bot writes them.
        overwrites = kwargs["overwrites"]
        assert overwrites[guild.default_role].send_messages is False
        assert overwrites[guild.default_role].read_messages is True
        assert overwrites[guild.me].send_messages is True
        assert overwrites[guild.me].embed_links is True

    @pytest.mark.asyncio
    async def test_new_channel_lands_in_the_category_it_was_given(self):
        """The caller passes the invoking channel's category, so a league's
        channels are created wherever the organiser ran the command."""
        category = MagicMock()
        category.name = "Lotus League 2026"
        made = channel_stub(333, STANDINGS.name)
        guild = guild_stub(created=made)
        await ensure_channel(guild, {}, STANDINGS, category)
        assert guild.create_text_channel.call_args.kwargs["category"] is category

    @pytest.mark.asyncio
    async def test_repairs_the_bots_write_permission_on_an_existing_channel(self):
        """A standings channel the bot can't post in is worse than none."""
        existing = channel_stub(111, STANDINGS.name)
        existing.overwrites_for.return_value = MagicMock(send_messages=False, embed_links=False)
        guild = guild_stub(channels=[existing])
        config = {"tournament": {STANDINGS.setting: "111"}}
        await ensure_channel(guild, config, STANDINGS, None)
        existing.set_permissions.assert_awaited_once()
        assert existing.set_permissions.await_args.kwargs["send_messages"] is True

    @pytest.mark.asyncio
    async def test_no_category_creates_at_the_top_level(self):
        """Running from an uncategorised channel is allowed, not an error."""
        made = channel_stub(333, STANDINGS.name)
        guild = guild_stub(created=made)
        await ensure_channel(guild, {}, STANDINGS, None)
        assert guild.create_text_channel.call_args.kwargs["category"] is None


class TestEnsurePairingsChannel:
    """Pairings is created like standings but stays writable: players click
    Play there and talk in the match threads hanging off it."""

    @pytest.mark.asyncio
    async def test_creates_a_writable_channel(self):
        made = channel_stub(444, PAIRINGS.name)
        guild = guild_stub(created=made)
        channel, created = await ensure_channel(guild, {}, PAIRINGS, None)
        assert (channel, created) == (made, True)
        kwargs = guild.create_text_channel.call_args.kwargs
        assert kwargs["name"] == PAIRINGS.name
        # Empty (not None): create_text_channel requires a dict, and an
        # empty one means "no overrides" -- the channel keeps guild defaults.
        assert kwargs["overwrites"] == {}

    @pytest.mark.asyncio
    async def test_adopts_an_explicitly_chosen_channel(self):
        chosen = channel_stub(555, "league-chat")
        guild = guild_stub(channels=[chosen])
        channel, created = await ensure_channel(guild, {}, PAIRINGS, None, chosen)
        assert (channel, created) == (chosen, False)
        guild.create_text_channel.assert_not_called()

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
        assert cog._destination(self._ctx(guild_stub(), here), STANDINGS.setting) is here

    def test_uses_the_configured_channel_when_set(self, monkeypatch):
        import cogs.tournament_commands as mod
        here = channel_stub(999, "general")
        configured = channel_stub(111, STANDINGS.name)
        guild = guild_stub(channels=[configured])
        monkeypatch.setattr(
            mod, "get_config",
            lambda _gid: {"tournament": {STANDINGS.setting: "111"}})
        cog = self._cog()
        assert cog._destination(self._ctx(guild, here), STANDINGS.setting) is configured

    def test_standings_and_pairings_resolve_independently(self, monkeypatch):
        import cogs.tournament_commands as mod
        here = channel_stub(999, "general")
        standings = channel_stub(111, STANDINGS.name)
        play = channel_stub(222, "tournament-play")
        guild = guild_stub(channels=[standings, play])
        monkeypatch.setattr(mod, "get_config", lambda _gid: {"tournament": {
            STANDINGS.setting: "111", PAIRINGS.setting: "222"}})
        cog, ctx = self._cog(), self._ctx(guild, here)
        assert cog._destination(ctx, STANDINGS.setting) is standings
        assert cog._destination(ctx, PAIRINGS.setting) is play

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
        channel = channel_stub(111, STANDINGS.name)
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
