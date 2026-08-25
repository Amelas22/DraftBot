"""A full category must never cost a draft its channels.

Discord caps a category at 50 channels while a guild allows 500, so a guild
running many drafts at once fills the draft category with room to spare
elsewhere. That used to fail silently and permanently: the shared Draft chat is
created and its id committed by create_team_channel's own session, then the team
channel raises, the outer transaction rolls back, and the next attempt sees
draft_chat_channel set and reports "rooms already existed". The draft keeps a
chat it cannot be played in, and nothing ever retries.

These drive ensure_channel directly. The behaviour is a property of making one
room, so it is tested where that happens rather than through a draft.
"""
from types import SimpleNamespace

import discord
import pytest

from helpers.draft_rooms import CATEGORY_CHANNEL_LIMIT, ensure_channel


def category_full():
    """The error Discord returns once a category holds 50 channels.

    Code 50035 is the generic "Invalid Form Body"; the cap is only identifiable
    from the nested parent_id error, which is why the production check keys on
    the code AND the field rather than on the message text. Copied from a real
    refusal, so the shape is not invented.
    """
    return discord.HTTPException(
        SimpleNamespace(status=400, reason="Bad Request"),
        {"code": 50035, "message": "Invalid Form Body",
         "errors": {"parent_id": {"_errors": [
             {"code": "CHANNEL_PARENT_MAX_CHANNELS",
              "message": "Maximum number of channels in category reached (50)"}]}}},
    )


class FakeCategory:
    def __init__(self, name, position=0, children=0):
        self.name = name
        self.position = position
        self.overwrites = {}
        # Only the COUNT matters -- the 50-channel cap is all this models.
        self.channels = [object()] * children


class FakeGuild:
    id = 4242

    def __init__(self, categories=(), full=(), create_category_error=None):
        self.categories = list(categories)
        # Names of categories at the cap. Modelled by NAME rather than by call
        # count because that is what the real limit attaches to: the same create
        # succeeds or fails purely on where it is pointed.
        self.full = set(full)
        self.create_category_error = create_category_error
        self.created_categories = []
        self.calls = []

    async def create_text_channel(self, **kwargs):
        self.calls.append(kwargs)
        category = kwargs.get("category")
        if category is not None and category.name in self.full:
            raise category_full()
        return SimpleNamespace(id=len(self.calls), name=kwargs["name"], category=category)

    async def create_category(self, name, **kwargs):
        if self.create_category_error:
            raise self.create_category_error
        self.created_categories.append(name)
        category = FakeCategory(name, position=kwargs.get("position", 0))
        category.overwrites = kwargs.get("overwrites", {})
        self.categories.append(category)
        return category


async def _make(guild, category):
    return await ensure_channel(guild, "text", "Red-Team-Chat-abc1", {}, category)


@pytest.mark.asyncio
async def test_a_full_category_creates_the_next_one():
    """The guild allows 500 channels; only the category caps at 50. Making the
    next one keeps the channels grouped AND keeps them coming, with no operator
    step."""
    base = FakeCategory("Draft Channels", position=1)
    guild = FakeGuild([base], full=("Draft Channels",))

    channel = await _make(guild, base)

    assert guild.created_categories == ["Draft Channels 2"]
    assert channel.category.name == "Draft Channels 2"


@pytest.mark.asyncio
async def test_an_existing_overflow_with_room_is_reused():
    """Otherwise every draft in a busy hour mints its own category and the guild
    fills with near-empty ones."""
    base = FakeCategory("Draft Channels", position=1)
    guild = FakeGuild([base, FakeCategory("Draft Channels 2", position=2, children=3)],
                      full=("Draft Channels",))

    channel = await _make(guild, base)

    assert guild.created_categories == [], "a category was created despite room"
    assert channel.category.name == "Draft Channels 2"


@pytest.mark.asyncio
async def test_a_full_overflow_leads_to_the_next_number():
    """Numbering hangs off the CONFIGURED category, so a full 'Draft Channels 2'
    leads to 'Draft Channels 3' and never to 'Draft Channels 2 2'."""
    base = FakeCategory("Draft Channels", position=1)
    guild = FakeGuild(
        [base, FakeCategory("Draft Channels 2", position=2, children=CATEGORY_CHANNEL_LIMIT)],
        full=("Draft Channels", "Draft Channels 2"))

    channel = await _make(guild, base)

    assert guild.created_categories == ["Draft Channels 3"]
    assert channel.category.name == "Draft Channels 3"


@pytest.mark.asyncio
async def test_the_new_category_inherits_the_base_category_s_permissions():
    """A draft category with restricted visibility must not spawn an open one."""
    base = FakeCategory("Draft Channels", position=1)
    base.overwrites = {"sentinel": "value"}
    guild = FakeGuild([base], full=("Draft Channels",))

    await _make(guild, base)

    made = next(c for c in guild.categories if c.name == "Draft Channels 2")
    assert made.overwrites == {"sentinel": "value"}


@pytest.mark.asyncio
async def test_a_refused_category_still_leaves_the_draft_playable():
    """No Manage Channels, or the guild's own 500-channel ceiling: fall through to
    uncategorised rather than costing the draft its room."""
    base = FakeCategory("Draft Channels", position=1)
    guild = FakeGuild([base], full=("Draft Channels",),
                      create_category_error=discord.Forbidden(
                          SimpleNamespace(status=403, reason="Forbidden"),
                          "Missing Permissions"))

    channel = await _make(guild, base)

    assert channel.category is None


@pytest.mark.asyncio
async def test_the_retry_keeps_the_permissions_it_was_given():
    """The fallback must not quietly widen who can read the channel."""
    base = FakeCategory("Draft Channels", position=1)
    guild = FakeGuild([base], full=("Draft Channels",))
    overwrites = {"marker": object()}

    await ensure_channel(guild, "text", "Red-Team-Chat-abc1", overwrites, base)

    assert all(call["overwrites"] is overwrites for call in guild.calls)


@pytest.mark.asyncio
async def test_an_unrelated_refusal_is_not_treated_as_a_full_category():
    """50035 is the generic "Invalid Form Body". Retrying every one of them in a
    different category would turn a real error into a confusing one."""
    base = FakeCategory("Draft Channels", position=1)
    guild = FakeGuild([base])

    async def refuse(**kwargs):
        guild.calls.append(kwargs)
        raise discord.HTTPException(
            SimpleNamespace(status=400, reason="Bad Request"),
            {"code": 50035, "message": "Invalid Form Body",
             "errors": {"name": {"_errors": [{"code": "BASE_TYPE_BAD_LENGTH",
                                              "message": "Must be 100 or fewer"}]}}})

    guild.create_text_channel = refuse
    with pytest.raises(discord.HTTPException):
        await _make(guild, base)
    assert len(guild.calls) == 1, "a non-category error should not be retried"


@pytest.mark.asyncio
async def test_the_log_can_tell_where_the_channel_landed():
    """A live run against a full category logged the category it ASKED for, five
    times, while every channel was in the overflow. The channel carries where it
    actually went, which is what the log reads."""
    base = FakeCategory("Draft Channels", position=1)
    guild = FakeGuild([base], full=("Draft Channels",))

    channel = await _make(guild, base)

    assert channel.category is not base
    assert channel.category.name == "Draft Channels 2"
