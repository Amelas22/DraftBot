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

from helpers.draft_rooms import (
    CATEGORY_CHANNEL_LIMIT, DRAFT_ROOM_COUNT, category_with_room, ensure_channel,
)


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


class CountingGuild:
    """Refuses on real OCCUPANCY rather than on a category's name.

    The name-based double below cannot express "room for one more but not three",
    because a category is either full for every call or for none. That is exactly
    the case where a draft's rooms can end up split across two categories, so it
    needs a double whose capacity changes as channels land in it.
    """

    id = 7

    def __init__(self, categories=()):
        self.categories = list(categories)

    async def create_text_channel(self, name, overwrites, category):
        if category is not None and len(category.channels) >= CATEGORY_CHANNEL_LIMIT:
            raise category_full()
        channel = SimpleNamespace(id=id(name), name=name, category=category)
        if category is not None:
            category.channels.append(channel)
        return channel

    async def create_category(self, name, overwrites=None, position=0):
        made = FakeCategory(name, position)
        self.categories.append(made)
        return made


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


# --- a draft's rooms belong together ----------------------------------------
# Capacity is checked for the WHOLE set a draft needs, not one room at a time.
# One room at a time, a category with a single free slot takes the shared chat
# and refuses both team channels, so the draft is split across two categories.

@pytest.mark.asyncio
async def test_a_category_with_room_for_the_whole_draft_is_used():
    base = FakeCategory("Draft Channels", position=1, children=10)
    guild = CountingGuild([base])

    assert await category_with_room(guild, base, DRAFT_ROOM_COUNT) is base


@pytest.mark.asyncio
async def test_a_category_with_room_for_only_some_of_them_is_not():
    """The case that splits a draft. 49 of 50 used is room for the chat and for
    neither team."""
    base = FakeCategory("Draft Channels", position=1,
                        children=CATEGORY_CHANNEL_LIMIT - 1)
    guild = CountingGuild([base])

    chosen = await category_with_room(guild, base, DRAFT_ROOM_COUNT)

    assert chosen is not base
    assert chosen.name == "Draft Channels 2"


@pytest.mark.asyncio
async def test_a_draft_s_rooms_all_land_in_one_category():
    """The behaviour that matters, end to end: three rooms, one category, even
    though the configured one had a slot free."""
    base = FakeCategory("Draft Channels", position=1,
                        children=CATEGORY_CHANNEL_LIMIT - 1)
    guild = CountingGuild([base])

    category = await category_with_room(guild, base, DRAFT_ROOM_COUNT)
    landed = [
        (await ensure_channel(guild, "text", f"{team}-Chat-abc1", {}, category)).category.name
        for team in ("Draft", "Red-Team", "Blue-Team")
    ]

    assert len(set(landed)) == 1, f"the draft was split across categories: {landed}"
    assert landed[0] == "Draft Channels 2"


@pytest.mark.asyncio
async def test_an_exactly_fitting_draft_still_uses_the_configured_category():
    """Room for exactly three is room enough -- the check must not be off by one
    and push a draft that fits into an overflow."""
    base = FakeCategory("Draft Channels", position=1,
                        children=CATEGORY_CHANNEL_LIMIT - DRAFT_ROOM_COUNT)
    guild = CountingGuild([base])

    assert await category_with_room(guild, base, DRAFT_ROOM_COUNT) is base


@pytest.mark.asyncio
async def test_no_configured_category_stays_no_category():
    """A guild that groups nothing must not have a category invented for it."""
    guild = CountingGuild([])

    assert await category_with_room(guild, None, DRAFT_ROOM_COUNT) is None


@pytest.mark.asyncio
async def test_the_whole_draft_fits_without_a_single_refusal():
    """The reserved capacity has to be real, not just chosen. With three rooms
    reserved in a category holding exactly three free slots, none of the creates
    may be refused -- this is the case the name-based double cannot express,
    because there the category's occupancy never changes as channels land."""
    base = FakeCategory("Draft Channels", position=1,
                        children=CATEGORY_CHANNEL_LIMIT - DRAFT_ROOM_COUNT)
    guild = CountingGuild([base])

    category = await category_with_room(guild, base, DRAFT_ROOM_COUNT)
    for team in ("Draft", "Red-Team", "Blue-Team"):
        await ensure_channel(guild, "text", f"{team}-Chat-abc1", {}, category)

    assert category is base, "a category with exactly enough room was passed over"
    assert len(base.channels) == CATEGORY_CHANNEL_LIMIT
    assert guild.categories == [base], "an overflow category was created needlessly"


@pytest.mark.asyncio
async def test_a_refused_overflow_numbers_off_the_configured_category():
    """A draft can be placed straight into "Draft Channels 2". If THAT is then
    refused, the next category must be "Draft Channels 3" -- numbering off the
    sibling would produce "Draft Channels 2 2"."""
    base = FakeCategory("Draft Channels", position=1)
    # Genuinely at capacity, not just named in `full`: overflow_category decides
    # by occupancy, so a double that disagreed with itself would send this down a
    # different path than the one under test.
    sibling = FakeCategory("Draft Channels 2", position=2,
                           children=CATEGORY_CHANNEL_LIMIT)
    guild = FakeGuild([base, sibling], full=("Draft Channels 2",))

    channel = await ensure_channel(guild, "text", "Red-Team-Chat-abc1", {}, sibling)

    assert guild.created_categories == ["Draft Channels 3"], (
        f"numbered off the sibling instead of the base: {guild.created_categories}")
    assert channel.category.name == "Draft Channels 3"
