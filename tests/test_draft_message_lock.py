"""Two redraws of one board never interleave, and the last one writes the newest roster.

A click handler and the library listener can redraw the same board at once. Each
reads the draft, fetches the message, edits the embed and writes it back; if
they interleave, the one that read first can write last and put an older roster
back on the board.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import discord
import pytest

import views


def _session(sign_ups):
    return SimpleNamespace(
        session_id="s1", friendly_id="bolt-1", cube="mycube", guild_id="999",
        draft_channel_id="111", message_id="222", session_type="random",
        sign_ups=sign_ups, packs_per_player=3, cards_per_pack=15,
    )


@pytest.mark.asyncio
async def test_two_redraws_of_one_board_serialize_and_the_newest_roster_wins():
    db = {"row": _session({"1": "Ava"})}
    a_fetching, release_a = asyncio.Event(), asyncio.Event()
    active = {"now": 0, "max": 0, "fetches": 0}
    written: "list[str]" = []

    embed = discord.Embed(title="Queue")
    embed.add_field(name="Sign-Ups:", value="**Players (0):**\nNo players yet.", inline=False)

    async def get_draft_session(_sid):
        row = db["row"]
        return _session(dict(row.sign_ups))

    async def fetch_message(_mid):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        active["fetches"] += 1
        if active["fetches"] == 1:
            a_fetching.set()
            await release_a.wait()      # the first redraw is slow on the network
        message = MagicMock()
        message.embeds = [embed.copy()]

        async def edit(*, embed):
            written.append(next(f.value for f in embed.fields if f.name == "Sign-Ups:"))
            active["now"] -= 1
        message.edit = edit
        return message

    channel = MagicMock()
    channel.fetch_message = fetch_message
    channel.guild = None
    bot = MagicMock()
    bot.get_channel.return_value = channel

    with patch.object(views, "get_draft_session", get_draft_session), \
         patch.object(views, "get_display_name_by_id", lambda uid, _g, stored: stored):
        first = asyncio.create_task(views.update_draft_message(bot, "s1"))
        await a_fetching.wait()
        db["row"] = _session({"1": "Ava", "2": "Ben"})     # a sign-up lands meanwhile
        second = asyncio.create_task(views.update_draft_message(bot, "s1"))
        for _ in range(20):
            await asyncio.sleep(0)
        release_a.set()
        await asyncio.gather(first, second)

    assert active["max"] == 1, "two redraws of one board ran at once"
    assert len(written) == 2 and "Ben" in written[-1], written


@pytest.mark.asyncio
async def test_redraws_of_different_boards_do_not_wait_for_each_other():
    gate = asyncio.Event()
    started: "list[str]" = []

    async def get_draft_session(sid):
        started.append(sid)
        if sid == "slow":
            await gate.wait()
        return None                      # nothing to draw: returns at once

    with patch.object(views, "get_draft_session", get_draft_session):
        slow = asyncio.create_task(views.update_draft_message(MagicMock(), "slow"))
        await asyncio.sleep(0)
        await asyncio.wait_for(views.update_draft_message(MagicMock(), "other"), 1)
        gate.set()
        await slow

    assert started == ["slow", "other"]
