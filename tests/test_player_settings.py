"""/settings: a player's own per-server settings, on one ephemeral message.

Each button flips what is stored (not what the message shows), saves it, and
redraws the same message. Hiding from the ladder also redraws the Skill Rating
Ladder board, and only that board.
"""
import asyncio
import dataclasses
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import discord
import pytest

import player_settings
import player_settings_view
from cogs.settings_commands import SettingsCommands
from database.db_session import AsyncSessionLocal
from models.leaderboard_message import LeaderboardMessage
from player_settings import Setting, Settings, flip, load_settings
from player_settings_view import SettingsView, settings_embed
from preference_service import get_anonymous_donors, get_player_dm_notification_preference
import utils
from utils import redraw_leaderboard

pytestmark = pytest.mark.asyncio


def _interaction(player="p1"):
    return SimpleNamespace(
        user=SimpleNamespace(id=player), client=MagicMock(),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()))


async def test_a_new_player_sees_the_defaults(test_db):
    s = await load_settings("p1", "g")
    assert (s.hide_from_ladder, s.anonymous) == (False, False)
    assert s.dms == await get_player_dm_notification_preference("p1", "g")


@pytest.mark.parametrize("setting, field", [
    (Setting.HIDE_FROM_LADDER, "hide_from_ladder"),
    (Setting.ANONYMOUS, "anonymous"),
    (Setting.DMS, "dms"),
])
async def test_each_flip_changes_only_its_own_setting(test_db, setting, field):
    before = await load_settings("p1", "g")
    new = await flip("p1", "g", setting)
    assert new is (not getattr(before, field))
    after = await load_settings("p1", "g")
    assert after == dataclasses.replace(before, **{field: new})


async def test_a_click_flips_the_stored_value_not_the_shown_one(test_db):
    stale = SettingsView("p1", "g", "Lounge", await load_settings("p1", "g"))
    await flip("p1", "g", Setting.DMS)  # another message changed it
    await stale.dms_button.callback(_interaction("p1"))
    assert (await load_settings("p1", "g")).dms == (await load_settings("p2", "g")).dms


async def test_the_embed_shows_on_and_off_and_buttons_match(test_db):
    s = Settings(dms=True, hide_from_ladder=False, anonymous=True)
    e = settings_embed("Lounge", s)
    assert e.title == "⚙️ Your settings: Lounge"
    assert "Settings are per server." in e.footer.text
    text = "\n".join(f.value for f in e.fields)
    assert "● On  DM me when a ready check starts" in text
    assert "○ Off  Hide me from the Skill Rating Ladder" in text
    assert "● On  Show me as Anonymous on donation pages" in text
    assert [f.name for f in e.fields] == ["Notifications", "Privacy"]
    v = SettingsView("p1", "g", "Lounge", s)
    assert v.dms_button.style is discord.ButtonStyle.success
    assert v.hide_button.style is discord.ButtonStyle.secondary
    assert v.anonymous_button.style is discord.ButtonStyle.success
    assert (v.dms_button.row, v.hide_button.row, v.anonymous_button.row) == (1, 2, 2)


async def test_a_click_saves_and_redraws_the_same_message(test_db):
    v = SettingsView("p1", "g", "Lounge", await load_settings("p1", "g"))
    i = _interaction("p1")
    await v.anonymous_button.callback(i)
    assert await get_anonymous_donors(["p1"], "g") == {"p1"}
    i.edit_original_response.assert_awaited_once()
    assert v.anonymous_button.style is discord.ButtonStyle.success


async def test_toggling_the_ladder_redraws_only_the_ladder(test_db, monkeypatch):
    redraw = AsyncMock()
    monkeypatch.setattr(player_settings_view, "redraw_leaderboard", redraw)
    view = SettingsView("p1", "g", "Lounge", await load_settings("p1", "g"))
    await view.hide_button.callback(_interaction("p1"))
    await asyncio.sleep(0)  # it is fire-and-forget
    redraw.assert_awaited_once_with(ANY, "g", "sr_ladder")


async def test_other_toggles_leave_the_ladder_alone(test_db, monkeypatch):
    redraw = AsyncMock()
    monkeypatch.setattr(player_settings_view, "redraw_leaderboard", redraw)
    view = SettingsView("p1", "g", "Lounge", await load_settings("p1", "g"))
    await view.anonymous_button.callback(_interaction("p1"))
    await asyncio.sleep(0)
    redraw.assert_not_awaited()


async def test_a_failed_save_says_so_and_changes_nothing(test_db, monkeypatch):
    monkeypatch.setattr(player_settings, "set_donation_anonymous", AsyncMock(return_value=False))
    i = _interaction("p1")
    view = SettingsView("p1", "g", "Lounge", await load_settings("p1", "g"))
    await view.anonymous_button.callback(i)
    assert "Couldn't save" in i.followup.send.await_args.args[0]
    i.edit_original_response.assert_not_awaited()


async def test_nobody_else_can_press_your_buttons(test_db):
    i = _interaction("intruder")
    view = SettingsView("p1", "g", "Lounge", await load_settings("p1", "g"))
    await view.dms_button.callback(i)
    assert "not for you" in i.response.send_message.await_args.args[0]
    assert (await load_settings("p1", "g")) == (await load_settings("p2", "g"))
    assert (await load_settings("intruder", "g")) == (await load_settings("p2", "g"))


async def test_an_expired_view_is_disabled_and_says_so(test_db):
    view = SettingsView("p1", "g", "Lounge", await load_settings("p1", "g"))
    opener = SimpleNamespace(edit_original_response=AsyncMock())
    view.opener = opener
    await view.on_timeout()
    assert all(getattr(item, "disabled", False) for item in view.children)
    assert "expired" in opener.edit_original_response.await_args.kwargs["content"]


async def test_settings_outside_a_server_says_where_to_run_it():
    ctx = SimpleNamespace(guild=None, author=SimpleNamespace(id=1), respond=AsyncMock())
    cog = SettingsCommands(MagicMock())
    await SettingsCommands.settings.callback(cog, ctx)
    assert "in a server" in ctx.respond.await_args.args[0]


async def test_redraw_without_a_leaderboard_message_does_nothing(test_db):
    await redraw_leaderboard(MagicMock(), "g", "sr_ladder")


async def test_redraw_edits_only_that_category(test_db, monkeypatch):
    async with AsyncSessionLocal() as session:
        session.add(LeaderboardMessage(
            guild_id="1", channel_id="2", message_id="10",
            sr_ladder_view_message_id="11", draft_record_view_message_id="12"))
        await session.commit()

    fetched_ids = []
    message = SimpleNamespace(edit=AsyncMock())

    async def fetch_message(mid):
        fetched_ids.append(mid)
        return message

    channel = SimpleNamespace(fetch_message=fetch_message)
    guild = SimpleNamespace(get_channel=lambda cid: channel if cid == 2 else None)
    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == 1 else None)
    monkeypatch.setattr(utils, "create_leaderboard_embed", AsyncMock(return_value="E"))

    await redraw_leaderboard(bot, "1", "sr_ladder")

    assert fetched_ids == [11]
    message.edit.assert_awaited_once_with(embed="E", view=None)  # pinned board: no selector
