"""/settings: a player's own per-server settings, on one ephemeral message.

Each button flips what is stored (not what the message shows), saves it, and
redraws the same message. Hiding from the ladder also redraws the Skill Rating
Ladder board, and only that board.
"""
import asyncio
import dataclasses
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import discord
import pytest

import player_settings
import player_settings_view
from cogs.settings_commands import SettingsCommands
from database.db_session import AsyncSessionLocal
from models.leaderboard_message import LeaderboardMessage
from models.draft_session import DraftSession
from models.stake import StakeInfo
from player_settings import (LiveResult, Setting, Settings, apply_cap_to_live_drafts, flip,
                             live_drafts, live_result_text, load_settings)
from player_settings_view import LIVE_FAILED, SettingsView, settings_embed
from preference_service import (get_anonymous_donors, get_player_bet_capping_preference,
                                get_player_dm_notification_preference, get_uses_library,
                                set_uses_library)
from services.library_prompt import AGREE_LABEL, COULD_NOT_CHECK
from services.library_reservation import reserve, still_reserved
from test_library_preference_prompt import (GUILD, PLAYER, _drafted, _labels, _lounge,
                                            _queue, _row, shelf)
from sqlalchemy import select
import views
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


# --- the stake cap, and the live sign-ups it carries into --------------------

BOT = MagicMock()


@pytest.fixture
def redraw(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(views, "update_draft_message", mock)
    return mock


@pytest.fixture
def money(monkeypatch):
    monkeypatch.setattr(player_settings, "is_money_server", lambda _g: True)


@pytest.fixture
def not_money(monkeypatch):
    monkeypatch.setattr(player_settings, "is_money_server", lambda _g: False)


async def draft(sid, players, guild="g", **fields):
    row = dict(session_id=sid, guild_id=guild, sign_ups={p: p for p in players},
               deletion_time=datetime.now() + timedelta(hours=3),
               draft_channel_id="chan", message_id=f"msg-{sid}")
    row.update(fields)
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(**row))
        await s.commit()


async def stake(sid, player, capped):
    async with AsyncSessionLocal() as s:
        s.add(StakeInfo(session_id=sid, player_id=player, max_stake=50, is_capped=capped))
        await s.commit()


async def stake_row(sid, player):
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(StakeInfo).where(
            StakeInfo.session_id == sid, StakeInfo.player_id == player))).scalars().first()


async def test_live_drafts_are_only_those_still_taking_sign_ups(test_db):
    await draft("open", ["p1"])
    await draft("teams", ["p1"], session_stage="teams")
    await draft("started", ["p1"], teams_start_time=datetime.now())
    await draft("matched", ["p1"], pool_matched_at=datetime.now())
    await draft("finished", ["p1"], session_stage="completed")
    await draft("abandoned", ["p1"], session_stage="abandoned")
    await draft("expired", ["p1"], deletion_time=datetime.now() - timedelta(minutes=1))
    await draft("elsewhere", ["p1"], guild="other")
    await draft("not_in", ["p2"])
    assert [d.session_id for d in await live_drafts("p1", "g")] == ["open"]


async def test_flipping_the_cap_changes_the_live_entry_and_redraws(test_db, redraw):
    await draft("open", ["p1"]); await stake("open", "p1", capped=True)
    result = await apply_cap_to_live_drafts(BOT, "p1", "g", False)
    assert [d.session_id for d in result.changed] == ["open"]
    assert (await stake_row("open", "p1")).is_capped is False
    redraw.assert_awaited_once_with(BOT, "open")


async def test_a_live_draft_without_a_stake_is_skipped(test_db, redraw):
    await draft("open", ["p1"])
    result = await apply_cap_to_live_drafts(BOT, "p1", "g", False)
    assert result == LiveResult(changed=[], failed=[])
    redraw.assert_not_awaited()
    assert live_result_text(result) == ""


async def test_one_failing_draft_does_not_stop_the_others(test_db, redraw):
    for sid in ("a", "b"):
        await draft(sid, ["p1"]); await stake(sid, "p1", capped=True)
    redraw.side_effect = [RuntimeError("discord"), None]
    result = await apply_cap_to_live_drafts(BOT, "p1", "g", False)
    assert len(result.changed) == 1 and len(result.failed) == 1
    text = live_result_text(result)
    assert "couldn't" in text.lower()
    assert "https://discord.com/channels/g/chan/msg-a" in text
    assert "https://discord.com/channels/g/chan/msg-b" in text


async def test_the_cap_button_saves_the_preference_and_reports_the_draft(test_db, redraw, money):
    await draft("open", ["p1"]); await stake("open", "p1", capped=True)
    v = SettingsView("p1", "g", "Lounge", await load_settings("p1", "g"))
    assert v.cap_button in v.children and v.cap_button.row == 0
    i = _interaction("p1")
    await v.cap_button.callback(i)
    assert await get_player_bet_capping_preference("p1", "g") is False
    assert (await stake_row("open", "p1")).is_capped is False
    sent = i.followup.send.await_args
    assert "discord.com/channels/g/chan/msg-open" in sent.args[0]
    assert sent.kwargs["ephemeral"] is True
    assert v.cap_button.style is discord.ButtonStyle.secondary
    i.edit_original_response.assert_awaited_once()


async def test_the_cap_button_with_no_live_draft_sends_no_followup(test_db, redraw, money):
    v = SettingsView("p1", "g", "Lounge", await load_settings("p1", "g"))
    i = _interaction("p1")
    await v.cap_button.callback(i)
    assert await get_player_bet_capping_preference("p1", "g") is False
    i.followup.send.assert_not_awaited()


async def test_the_cap_shows_on_a_money_server(test_db, money):
    s = await load_settings("p1", "g")
    assert s.stake_cap is True
    e = settings_embed("Lounge", s)
    assert e.fields[0].name == "Drafting"
    assert "● On  Cap my stake to the other side" in e.fields[0].value
    assert SettingsView("p1", "g", "Lounge", s).cap_button.label == "Stake cap"


async def test_no_cap_row_off_a_money_server(test_db, not_money):
    s = await load_settings("p1", "g")
    assert s.stake_cap is None
    v = SettingsView("p1", "g", "Lounge", s)
    assert v.cap_button not in v.children
    assert "Drafting" not in [f.name for f in settings_embed("Lounge", s).fields]


async def test_the_panel_still_changes_the_entry(test_db, redraw):
    await draft("open", ["p1"]); await stake("open", "p1", capped=True)
    i = SimpleNamespace(client=BOT, response=SimpleNamespace(send_message=AsyncMock()))
    await views.BetCapToggleButton("open").update_cap_status(i, "p1", "g", is_capped=False)
    assert (await stake_row("open", "p1")).is_capped is False
    assert await get_player_bet_capping_preference("p1", "g") is False
    redraw.assert_awaited_once_with(BOT, "open")
    assert "Your entry cap is now OFF" in i.response.send_message.await_args.args[0]


async def test_the_panel_without_an_entry_says_so(test_db, redraw):
    await draft("open", ["p1"])
    i = SimpleNamespace(client=BOT, response=SimpleNamespace(send_message=AsyncMock()))
    await views.BetCapToggleButton("open").update_cap_status(i, "p1", "g", is_capped=False)
    assert i.response.send_message.await_args.args[0] == "Error: your entry could not be found."
    assert await get_player_bet_capping_preference("p1", "g") is True
    redraw.assert_not_awaited()


async def test_a_failed_message_edit_still_redraws_the_ladder(test_db, monkeypatch):
    redraw = AsyncMock()
    monkeypatch.setattr(player_settings_view, "redraw_leaderboard", redraw)
    view = SettingsView("p1", "g", "Lounge", await load_settings("p1", "g"))
    i = _interaction("p1")
    i.edit_original_response.side_effect = RuntimeError("discord")
    with pytest.raises(RuntimeError):
        await view.hide_button.callback(i)
    await asyncio.sleep(0)
    redraw.assert_awaited_once_with(ANY, "g", "sr_ladder")


async def test_a_failed_panel_redraw_keeps_the_entry_and_the_preference(test_db, redraw):
    await draft("open", ["p1"]); await stake("open", "p1", capped=True)
    redraw.side_effect = RuntimeError("discord")
    i = SimpleNamespace(client=BOT, response=SimpleNamespace(send_message=AsyncMock()))
    with pytest.raises(RuntimeError):
        await views.BetCapToggleButton("open").update_cap_status(i, "p1", "g", is_capped=False)
    assert (await stake_row("open", "p1")).is_capped is False
    assert await get_player_bet_capping_preference("p1", "g") is False


async def test_a_cap_live_step_that_raises_still_keeps_the_preference(test_db, money, monkeypatch):
    monkeypatch.setattr(player_settings_view, "apply_cap_to_live_drafts",
                        AsyncMock(side_effect=RuntimeError("db")))
    v = SettingsView("p1", "g", "Lounge", await load_settings("p1", "g"))
    i = _interaction("p1")
    await v.cap_button.callback(i)
    assert await get_player_bet_capping_preference("p1", "g") is False
    i.edit_original_response.assert_awaited_once()
    sent = i.followup.send.await_args
    assert sent.args[0] == LIVE_FAILED and sent.kwargs["ephemeral"] is True


# --- the card library, and the live sign-ups it carries into -----------------

async def test_the_library_row_shows_only_where_the_player_may_borrow(test_db, shelf):
    assert (await load_settings(PLAYER, GUILD)).library_offered is False   # no library
    await _lounge(); await _drafted(PLAYER, 3)
    assert (await load_settings(PLAYER, GUILD)).library_offered is True


async def test_a_library_lookup_failure_leaves_the_row_out(test_db, monkeypatch):
    monkeypatch.setattr(player_settings, "rental_library_for", AsyncMock(side_effect=RuntimeError))
    s = await load_settings(PLAYER, GUILD)
    assert s.library_offered is False and s.dms in (True, False)        # the rest still loads


async def test_not_set_goes_on_then_off(test_db, shelf):
    await _lounge(); await _drafted(PLAYER, 3)
    assert "Not set (you'll be asked at sign-up)" in str(settings_embed("L", await load_settings(PLAYER, GUILD)).to_dict())
    assert await flip(PLAYER, GUILD, Setting.LIBRARY) is True
    assert await flip(PLAYER, GUILD, Setting.LIBRARY) is False
    assert await get_uses_library(PLAYER, GUILD) is False


async def test_the_library_line_and_button_follow_the_stored_answer(test_db):
    for stored, mark, style in ((True, "● On", discord.ButtonStyle.success),
                                (False, "○ Off", discord.ButtonStyle.secondary),
                                (None, "○ Not set (you'll be asked at sign-up)",
                                 discord.ButtonStyle.secondary)):
        s = Settings(dms=True, hide_from_ladder=False, anonymous=False,
                     library=stored, library_offered=True)
        drafting = settings_embed("L", s).fields[0]
        assert drafting.name == "Drafting"
        assert f"{mark}  Offer me a library deck" in drafting.value
        v = SettingsView(PLAYER, GUILD, "L", s)
        assert v.library_button in v.children and v.library_button.row == 0
        assert v.library_button.label == "Card library" and v.library_button.style is style


async def test_no_library_row_where_none_is_offered(test_db):
    s = Settings(dms=True, hide_from_ladder=False, anonymous=False, library=True)
    v = SettingsView(PLAYER, GUILD, "L", s)
    assert v.library_button not in v.children
    assert "Offer me a library deck" not in str(settings_embed("L", s).to_dict())


async def test_switching_on_holds_the_copy_in_the_live_draft(test_db, shelf):
    await _lounge(stock={"Swamp": 4}); await _drafted(PLAYER, 3); await _queue([PLAYER])
    v = SettingsView(PLAYER, GUILD, "L", await load_settings(PLAYER, GUILD)); i = _interaction(PLAYER)
    await v.library_button.callback(i)
    sent = i.followup.send.await_args
    assert "held for this draft" in sent.args[0] and AGREE_LABEL in _labels(sent.kwargs["view"])


async def test_switching_off_lets_the_live_copy_go(test_db, shelf):
    await _lounge(stock={"Swamp": 4}); await _drafted(PLAYER, 3)
    await set_uses_library(PLAYER, GUILD, True); await _queue([PLAYER])
    assert (await reserve("q1")).status == "reserved"
    i = _interaction(PLAYER)
    await SettingsView(PLAYER, GUILD, "L", await load_settings(PLAYER, GUILD)).library_button.callback(i)
    assert not await still_reserved(await _row())
    i.followup.send.assert_not_awaited()


async def test_a_draft_that_cannot_be_checked_says_so_and_the_rest_go_on(test_db, shelf, monkeypatch):
    await _lounge(stock={"Swamp": 4}); await _drafted(PLAYER, 3)
    await _queue([PLAYER], sid="q1"); await _queue([PLAYER], sid="q2")
    real = player_settings.reserve_and_say

    async def flaky(player_id, d, library, **kw):
        if d.session_id == "q1":
            raise RuntimeError("cube list")
        return await real(player_id, d, library, **kw)
    monkeypatch.setattr(player_settings, "reserve_and_say", flaky)
    await set_uses_library(PLAYER, GUILD, True)
    results = await player_settings.apply_library_to_live_drafts(PLAYER, GUILD)
    assert [(d.session_id, text, view) for d, text, view in results][0] == ("q1", COULD_NOT_CHECK, None)
    assert results[1][0].session_id == "q2" and results[1][1]


async def test_a_draft_of_a_cube_the_library_does_not_lend_is_left_out(test_db, shelf):
    await _lounge(stock={"Swamp": 4}); await _drafted(PLAYER, 3)
    await _queue([PLAYER], sid="other", cube="not-lent")
    await set_uses_library(PLAYER, GUILD, True)
    assert await player_settings.apply_library_to_live_drafts(PLAYER, GUILD) == []


async def test_a_library_live_step_that_raises_still_keeps_the_preference(test_db, shelf, monkeypatch):
    await _lounge(); await _drafted(PLAYER, 3)
    monkeypatch.setattr(player_settings_view, "apply_library_to_live_drafts",
                        AsyncMock(side_effect=RuntimeError("db")))
    v = SettingsView(PLAYER, GUILD, "L", await load_settings(PLAYER, GUILD))
    i = _interaction(PLAYER)
    await v.library_button.callback(i)
    assert await get_uses_library(PLAYER, GUILD) is True
    i.edit_original_response.assert_awaited_once()
    sent = i.followup.send.await_args
    assert sent.args[0] == LIVE_FAILED and sent.kwargs["ephemeral"] is True
