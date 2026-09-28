"""A staked draft's embed: that the money appears on it at all.

Two prod bugs, one masking the other, both landed in the prize-pool migration
(2ab11bf):

1. `_add_stake_info_to_embed` was gated on a `stake_info_by_player` dict that
   nothing populated any more, so it returned early and the field vanished from
   every staked draft's embed.
2. Behind that dead gate, a parser re-derived the regime by splitting each line
   on " vs ". The pool renders one line per player (`**Alice**: 100 tix`) where
   the retired tiered matcher rendered pairs (`Alice vs Bob: 100 tix`), so with
   the gate open it raised IndexError.

The dead gate hid the broken parser, so the symptom was a missing field rather
than a crash -- and a missing field reads as "this draft has no entries".
"""
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from services import team_creator


def _session():
    return SimpleNamespace(session_id="s1", sign_ups={"1": "Alice", "2": "Bob"})


def _field(embed):
    return next((f for f in embed.fields if "Entries" in f.name), None)


async def _render(lines, total):
    embed = discord.Embed(title="t")
    with patch.object(team_creator, "get_formatted_stake_pairs",
                      AsyncMock(return_value=(lines, total))):
        await team_creator._add_stake_info_to_embed(embed, _session())
    return embed


@pytest.mark.asyncio
async def test_the_entries_reach_the_embed():
    """One line per player is the live format, and it must render."""
    embed = await _render(["**Alice**: 100 tix", "**Bob**: 60 tix"], 160)

    field = _field(embed)
    assert field is not None, "the field vanished -- the gate is dead again"
    assert "**Alice**: 100 tix" in field.value
    assert "160 tix" in field.name


@pytest.mark.asyncio
async def test_a_retired_tiered_draft_still_renders():
    """Drafts paired under the old matcher keep their pairings until they
    finish, and they come through the same formatter."""
    embed = await _render(["**Alice** vs **Bob**: 100 tix"], 100)

    assert "**Alice** vs **Bob**: 100 tix" in _field(embed).value


@pytest.mark.asyncio
async def test_a_player_named_like_a_pairing_is_left_alone():
    """The regime used to be guessed from the text of the line, so a display
    name containing " vs " was read as two players and its markdown mangled
    (`****Alice** vs **Bob****: 100 tix`). Nothing parses the line now."""
    embed = await _render(["**Alice vs Bob**: 100 tix"], 100)

    assert "**Alice vs Bob**: 100 tix" in _field(embed).value


@pytest.mark.asyncio
async def test_nothing_is_added_when_there_is_nothing_at_risk():
    assert not (await _render([], 0)).fields
