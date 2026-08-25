"""The room-making unit, tested without a draft.

That these need no PersistentView, no database and no session is the point of
the extraction: the same assertions previously required faking all three,
because permission construction and channel creation lived inside a method that
also accumulated channel_ids and committed them.
"""
from types import SimpleNamespace

import pytest

from helpers.draft_rooms import (
    ensure_channel,
    resolve_category,
    team_channel_name,
    team_overwrites,
    team_voice_name,
)


class FakeRole:
    """Hashable stand-in: overwrites is keyed by role/member objects, and
    SimpleNamespace defines __eq__ so it cannot be a key."""

    def __init__(self, name, bot_id=None):
        self.name = name
        # `tags.bot_id` is what distinguishes the role Discord creates for a bot
        # from a vanity role someone named to look like one.
        self.tags = SimpleNamespace(bot_id=bot_id) if bot_id else None


class FakeGuild:
    name = "Test Guild"

    def __init__(self, roles=(), categories=()):
        self.me = FakeRole("bot")
        self.default_role = FakeRole("everyone")
        self.roles = list(roles)
        self.categories = list(categories)
        self.created = []

    async def create_text_channel(self, **kwargs):
        self.created.append(("text", kwargs))
        return SimpleNamespace(id=1, name=kwargs["name"])

    async def create_voice_channel(self, **kwargs):
        self.created.append(("voice", kwargs))
        return SimpleNamespace(id=2, name=kwargs["name"])


CONFIG = {"categories": {"draft": "Draft Channels"}, "roles": {"admin": "Admin"}}


def test_names_are_derived_only_from_the_team_and_the_draft():
    """Which is what lets a later run recognise a room it already made."""
    assert team_channel_name("Red-Team", "abc1") == "Red-Team-Chat-abc1"
    assert team_voice_name("Red-Team", "abc1") == "Red-Team-Voice-abc1"


def test_an_unconfigured_category_is_no_category_rather_than_an_error():
    guild = FakeGuild(categories=[SimpleNamespace(name="Draft Channels")])

    assert resolve_category(guild, CONFIG, "draft").name == "Draft Channels"
    assert resolve_category(guild, CONFIG, "voice") is None


def test_a_configured_category_the_guild_does_not_have_is_no_category():
    """A stale name in config must not stop a draft getting its rooms."""
    guild = FakeGuild(categories=[])

    assert resolve_category(guild, CONFIG, "draft") is None


def test_a_team_room_starts_closed_and_opens_only_to_its_team():
    alice = FakeRole("alice")
    guild = FakeGuild(roles=[FakeRole("Admin")])

    overwrites = team_overwrites(guild, CONFIG, "Red-Team", [alice], [])

    assert overwrites[guild.default_role].read_messages is False
    assert overwrites[alice].read_messages is True
    # The admin role gets the shared chat, never a team's private room.
    assert not any(getattr(k, "name", None) == "Admin" for k in overwrites)


def test_the_shared_chat_also_opens_to_the_admin_role():
    admin = FakeRole("Admin")
    guild = FakeGuild(roles=[admin])

    overwrites = team_overwrites(guild, CONFIG, "Draft", [], [])

    assert overwrites[admin].read_messages is True


def test_only_a_real_bot_role_gets_draft_access():
    """A vanity role named after a bot cannot be used to read private team
    channels: only the managed role Discord creates for an invited bot counts,
    and that one cannot be handed to a human."""
    real = FakeRole("Scryfall", bot_id=42)
    impostor = FakeRole("Scryfall")
    guild = FakeGuild(roles=[impostor, real])

    overwrites = team_overwrites(guild, CONFIG, "Red-Team", [], ["Scryfall"])

    assert real in overwrites
    assert impostor not in overwrites


@pytest.mark.asyncio
async def test_ensure_channel_makes_the_kind_it_was_asked_for():
    guild = FakeGuild()
    category = SimpleNamespace(name="Draft Channels")

    await ensure_channel(guild, "text", "Red-Team-Chat-abc1", {}, category)
    await ensure_channel(guild, "voice", "Red-Team-Voice-abc1", {}, category)

    assert [kind for kind, _ in guild.created] == ["text", "voice"]
    assert guild.created[0][1]["category"] is category


@pytest.mark.asyncio
async def test_a_voice_room_carries_its_text_room_s_permissions():
    """Passed as the same object rather than rebuilt, so the two cannot drift:
    a team that can read its own channel can talk in exactly the same room."""
    guild = FakeGuild()
    overwrites = team_overwrites(guild, CONFIG, "Red-Team", [], [])

    await ensure_channel(guild, "text", "t", overwrites, None)
    await ensure_channel(guild, "voice", "v", overwrites, None)

    assert guild.created[0][1]["overwrites"] is guild.created[1][1]["overwrites"]
