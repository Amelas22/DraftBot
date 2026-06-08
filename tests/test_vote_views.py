"""Characterization tests for the majority-vote views in cogs/draft_control.py.

These lock the per-view appearance/wording and the shared majority-vote
behavior so the BaseVoteView extraction can be verified to preserve behavior.
"""
import discord
import pytest
from unittest.mock import AsyncMock, MagicMock

from cogs.draft_control import (
    ScrapVoteView,
    LogReleaseVoteView,
    ReplaceWithBotsVoteView,
    AbandonVoteView,
)

# (class, expected config) for every majority-vote view.
SPECS = [
    (ScrapVoteView, {
        "title": "Draft Cancellation Vote",
        "desc": "Vote to cancel the current draft.",
        "color": discord.Color.red(),
        "yes_label": "Yes, Cancel Draft", "yes_style": discord.ButtonStyle.danger,
        "no_label": "No, Continue Draft", "no_style": discord.ButtonStyle.green,
        "yes_status": "✅ Voted to Cancel", "no_status": "❌ Voted to Continue",
        "needed": "votes needed to cancel",
    }),
    (LogReleaseVoteView, {
        "title": "Draft Logs Release Vote",
        "desc": "Vote to release the draft logs early.",
        "color": discord.Color.blue(),
        "yes_label": "Yes, Release Logs", "yes_style": discord.ButtonStyle.primary,
        "no_label": "No, Keep Logs Private", "no_style": discord.ButtonStyle.secondary,
        "yes_status": "✅ Voted to Release", "no_status": "❌ Voted to Keep Private",
        "needed": "votes needed to release logs",
    }),
    (ReplaceWithBotsVoteView, {
        "title": "Replace Disconnected Players Vote",
        "desc": "Vote to replace disconnected players with bots.",
        "color": discord.Color.blue(),
        "yes_label": "Yes, Replace with Bots", "yes_style": discord.ButtonStyle.primary,
        "no_label": "No, Wait for Players", "no_style": discord.ButtonStyle.secondary,
        "yes_status": "✅ Replace with Bots", "no_status": "❌ Wait for Players",
        "needed": "votes needed to replace",
    }),
    (AbandonVoteView, {
        "title": "Draft Abandonment Vote",
        "desc": "Vote to abandon the current draft. All match results will be voided.",
        "color": discord.Color.red(),
        "yes_label": "Yes, Abandon Draft", "yes_style": discord.ButtonStyle.danger,
        "no_label": "No, Keep Draft", "no_style": discord.ButtonStyle.green,
        "yes_status": "✅ Voted to Abandon", "no_status": "❌ Voted to Keep",
        "needed": "votes needed to abandon",
    }),
]

CLASSES = [cls for cls, _ in SPECS]


def make_guild():
    guild = MagicMock()
    def get_member(uid):
        member = MagicMock()
        member.display_name = f"P{uid}"
        return member
    guild.get_member.side_effect = get_member
    return guild


@pytest.mark.asyncio
@pytest.mark.parametrize("cls,spec", SPECS)
async def test_button_appearance(cls, spec):
    view = cls("s1", ["1", "2", "3"])
    assert view.yes_button.label == spec["yes_label"]
    assert view.yes_button.style == spec["yes_style"]
    assert view.no_button.label == spec["no_label"]
    assert view.no_button.style == spec["no_style"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cls,spec", SPECS)
async def test_status_embed_content(cls, spec):
    view = cls("s1", ["1", "2", "3"])
    view.votes = {"1": True, "2": False, "3": None}
    embed = await view.generate_status_embed(make_guild())

    assert embed.title == spec["title"]
    assert embed.description == spec["desc"]
    assert embed.color == spec["color"]

    text = "\n".join(f.value for f in embed.fields)
    assert spec["yes_status"] in text
    assert spec["no_status"] in text
    assert "⏳ Not Voted" in text
    assert spec["needed"] in text


@pytest.mark.asyncio
@pytest.mark.parametrize("cls,spec", SPECS)
async def test_timeout_embed(cls, spec):
    view = cls("s1", ["1", "2"])
    view.message = MagicMock()
    view.message.edit = AsyncMock()

    await view.on_timeout()

    assert view.complete.is_set()
    embed = view.message.edit.call_args.kwargs["embed"]
    assert embed.title == f"{spec['title']} - Ended"
    assert spec["needed"] in embed.fields[0].value


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", CLASSES)
async def test_majority_threshold_even(cls):
    view = cls("s1", ["1", "2", "3", "4"])
    view.votes = {"1": True, "2": True, "3": None, "4": None}
    passed, yes, total = view.get_vote_result()
    assert (yes, total) == (2, 4)
    assert not passed  # need 3 of 4
    view.votes["3"] = True
    assert view.get_vote_result()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", CLASSES)
async def test_majority_threshold_odd(cls):
    view = cls("s1", ["1", "2", "3"])
    view.votes = {"1": True, "2": True, "3": False}
    passed, yes, total = view.get_vote_result()
    assert passed and yes == 2 and total == 3  # need 2 of 3
