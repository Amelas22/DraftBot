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

from conftest import GUILD, PLAYER, _drafted, _labels, _lounge, _queue, _row
import player_settings
import player_settings_view
from cogs.settings_commands import SettingsCommands
from database.db_session import AsyncSessionLocal
from models.leaderboard_message import LeaderboardMessage
from models.draft_session import DraftSession
from models.stake import StakeInfo
from player_settings import (LiveResult, Setting, Settings, apply_cap_to_live_drafts, flip,
                             live_drafts, live_result_text, load_settings)
from player_settings_view import (EXPIRED, LIVE_FAILED, NOTHING_AFFECTED, SettingsView,
                                  settings_embed)
from preference_service import (get_anonymous_donors, get_player_bet_capping_preference,
                                get_player_dm_notification_preference, get_uses_library,
                                set_uses_library)
from services.library_prompt import AGREE_LABEL, COULD_NOT_CHECK
from services.library_reservation import reserve, still_reserved
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
    other = await flip("p1", "g", Setting.DMS)  # another message changed it
    await stale.dms_button.callback(_interaction("p1"))
    assert await get_player_dm_notification_preference("p1", "g") is (not other)


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
    get, _put = player_settings._STORE[Setting.ANONYMOUS]
    monkeypatch.setitem(player_settings._STORE, Setting.ANONYMOUS,
                        (get, AsyncMock(return_value=False)))
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


def _board_bot(fetch_message, embed_links=True):
    """A bot that sees guild 1 and its leaderboard channel 2."""
    perms = SimpleNamespace(send_messages=True, embed_links=embed_links,
                            read_message_history=True)
    channel = SimpleNamespace(name="leaderboard", fetch_message=fetch_message,
                              permissions_for=lambda _member: perms)
    guild = SimpleNamespace(get_channel=lambda cid: channel if cid == 2 else None,
                            get_member=lambda _uid: "bot-member")
    return SimpleNamespace(user=SimpleNamespace(id=99),
                           get_guild=lambda gid: guild if gid == 1 else None)


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

    bot = _board_bot(fetch_message)
    monkeypatch.setattr(utils, "create_leaderboard_embed", AsyncMock(return_value="E"))

    await redraw_leaderboard(bot, "1", "sr_ladder")

    assert fetched_ids == [11]
    message.edit.assert_awaited_once_with(embed="E", view=None)  # pinned board: no selector


async def test_redraw_without_permission_in_the_channel_does_nothing(test_db, monkeypatch):
    async with AsyncSessionLocal() as session:
        session.add(LeaderboardMessage(guild_id="1", channel_id="2", message_id="10",
                                       sr_ladder_view_message_id="11"))
        await session.commit()
    fetch_message = AsyncMock()
    monkeypatch.setattr(utils, "create_leaderboard_embed", AsyncMock(return_value="E"))
    await redraw_leaderboard(_board_bot(fetch_message, embed_links=False), "1", "sr_ladder")
    fetch_message.assert_not_awaited()


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


async def test_one_failing_draft_does_not_stop_the_others(test_db, redraw, monkeypatch):
    for sid in ("a", "b"):
        await draft(sid, ["p1"]); await stake(sid, "p1", capped=True)
    real = player_settings.apply_cap_to_entry

    async def fails_on_a(session_id, player_id, is_capped):
        if session_id == "a":
            raise RuntimeError("db")
        return await real(session_id, player_id, is_capped)
    monkeypatch.setattr(player_settings, "apply_cap_to_entry", fails_on_a)
    result = await apply_cap_to_live_drafts(BOT, "p1", "g", False)
    assert [d.session_id for d in result.changed] == ["b"]
    assert [d.session_id for d in result.failed] == ["a"]
    assert (await stake_row("a", "p1")).is_capped is True
    assert (await stake_row("b", "p1")).is_capped is False
    text = live_result_text(result)
    assert "couldn't" in text.lower()
    assert "https://discord.com/channels/g/chan/msg-a" in text
    assert "https://discord.com/channels/g/chan/msg-b" in text


async def test_a_failed_board_redraw_still_counts_the_entry_as_changed(test_db, redraw):
    await draft("open", ["p1"]); await stake("open", "p1", capped=True)
    redraw.side_effect = RuntimeError("discord")
    result = await apply_cap_to_live_drafts(BOT, "p1", "g", False)
    assert [d.session_id for d in result.changed] == ["open"] and result.failed == []
    assert (await stake_row("open", "p1")).is_capped is False


async def close(sid, **fields):
    """The draft moves on after live_drafts read it, before the write."""
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(DraftSession).where(
            DraftSession.session_id == sid))).scalars().one()
        for k, v in fields.items():
            setattr(row, k, v)
        await s.commit()


CLOSINGS = [dict(session_stage="teams"), dict(teams_start_time=datetime.now()),
            dict(pool_matched_at=datetime.now())]


@pytest.mark.parametrize("closing", CLOSINGS)
async def test_a_draft_that_moved_on_before_the_write_keeps_its_entry(test_db, closing):
    await draft("open", ["p1"]); await stake("open", "p1", capped=True)
    await close("open", **closing)
    assert await player_settings.apply_cap_to_entry("open", "p1", False) == "closed"
    assert (await stake_row("open", "p1")).is_capped is True


async def test_the_entry_write_says_whether_it_changed_or_found_none(test_db):
    await draft("open", ["p1"]); await stake("open", "p1", capped=True)
    await draft("empty", ["p1"])
    assert await player_settings.apply_cap_to_entry("open", "p1", False) == "changed"
    assert await player_settings.apply_cap_to_entry("empty", "p1", False) == "no_entry"


async def test_a_draft_closing_mid_settings_is_neither_changed_nor_failed(
        test_db, redraw, monkeypatch):
    await draft("open", ["p1"]); await stake("open", "p1", capped=True)
    real = player_settings.live_drafts

    async def then_teams(player_id, guild_id):
        drafts = await real(player_id, guild_id)
        await close("open", session_stage="teams")
        return drafts
    monkeypatch.setattr(player_settings, "live_drafts", then_teams)
    result = await apply_cap_to_live_drafts(BOT, "p1", "g", False)
    assert result == LiveResult(changed=[], failed=[])
    assert (await stake_row("open", "p1")).is_capped is True
    redraw.assert_not_awaited()


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


async def test_the_cap_button_with_no_live_draft_says_none_were_affected(test_db, redraw, money):
    v = SettingsView("p1", "g", "Lounge", await load_settings("p1", "g"))
    i = _interaction("p1")
    await v.cap_button.callback(i)
    assert await get_player_bet_capping_preference("p1", "g") is False
    sent = i.followup.send.await_args
    assert sent.args[0] == NOTHING_AFFECTED and sent.kwargs["ephemeral"] is True
    i.followup.send.assert_awaited_once()


async def test_a_click_keeps_a_fresh_token_for_the_timeout(test_db):
    v = SettingsView("p1", "g", "Lounge", await load_settings("p1", "g"))
    v.opener = _interaction("p1")
    click = _interaction("p1")
    await v.dms_button.callback(click)
    assert v.opener is click
    await v.on_timeout()
    click.edit_original_response.assert_awaited_with(content=EXPIRED, view=v)


async def test_two_quick_cap_clicks_leave_the_preference_and_the_entry_agreeing(
        test_db, redraw, money, monkeypatch):
    await draft("open", ["p1"]); await stake("open", "p1", capped=True)
    real = player_settings_view.apply_cap_to_live_drafts
    calls = 0

    async def slow_first(bot, player_id, guild_id, is_capped):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0.05)   # the first click's live write lands last
        return await real(bot, player_id, guild_id, is_capped)
    monkeypatch.setattr(player_settings_view, "apply_cap_to_live_drafts", slow_first)
    v = SettingsView("p1", "g", "Lounge", await load_settings("p1", "g"))
    await asyncio.gather(v.cap_button.callback(_interaction("p1")),
                         v.cap_button.callback(_interaction("p1")))
    preference = await get_player_bet_capping_preference("p1", "g")
    assert preference is True                      # flipped twice
    assert (await stake_row("open", "p1")).is_capped is preference


async def test_the_cap_shows_on_a_money_server(test_db, money):
    s = await load_settings("p1", "g")
    assert s.stake_cap is True
    e = settings_embed("Lounge", s)
    assert e.fields[0].name == "Drafting"
    assert "● On  Cap my entry to my share of my team" in e.fields[0].value
    assert SettingsView("p1", "g", "Lounge", s).cap_button.label == "Entry cap"


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


async def test_the_panel_on_a_draft_that_has_started_changes_nothing(test_db, redraw):
    await draft("open", ["p1"]); await stake("open", "p1", capped=True)
    await close("open", session_stage="teams")
    i = SimpleNamespace(client=BOT, response=SimpleNamespace(send_message=AsyncMock()))
    await views.BetCapToggleButton("open").update_cap_status(i, "p1", "g", is_capped=False)
    assert i.response.send_message.await_args.args[0] == (
        "This draft has already started, so your entry can't change now.")
    assert (await stake_row("open", "p1")).is_capped is True
    assert await get_player_bet_capping_preference("p1", "g") is True
    redraw.assert_not_awaited()


async def test_the_panel_says_when_the_preference_did_not_save(test_db, redraw, monkeypatch):
    import preference_service
    await draft("open", ["p1"]); await stake("open", "p1", capped=True)
    monkeypatch.setattr(preference_service, "update_player_bet_capping_preference",
                        AsyncMock(return_value=False))
    i = SimpleNamespace(client=BOT, response=SimpleNamespace(send_message=AsyncMock()))
    await views.BetCapToggleButton("open").update_cap_status(i, "p1", "g", is_capped=False)
    text = i.response.send_message.await_args.args[0]
    assert (await stake_row("open", "p1")).is_capped is False
    assert "Your entry cap is now OFF" in text
    assert "⚠️ I couldn't remember this for future drafts; try again." in text
    assert "This preference is remembered for future drafts." not in text


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
    assert sent.args[0].startswith(f"https://discord.com/channels/{GUILD}/chan/msg-q1\n")


async def test_switching_on_with_no_live_draft_says_none_were_affected(test_db, shelf):
    await _lounge(stock={"Swamp": 4}); await _drafted(PLAYER, 3)
    v = SettingsView(PLAYER, GUILD, "L", await load_settings(PLAYER, GUILD)); i = _interaction(PLAYER)
    await v.library_button.callback(i)
    assert await get_uses_library(PLAYER, GUILD) is True
    i.followup.send.assert_awaited_once_with(NOTHING_AFFECTED, ephemeral=True)


async def test_a_library_note_without_board_ids_has_no_link(test_db, shelf):
    await _lounge(stock={"Swamp": 4}); await _drafted(PLAYER, 3); await _queue([PLAYER])
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(DraftSession))).scalars().one()
        row.message_id = None
        await s.commit()
    await set_uses_library(PLAYER, GUILD, True)
    [(text, _v)] = await player_settings.apply_library_to_live_drafts(PLAYER, GUILD)
    assert text.startswith("🏛️")


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
    first, second = await player_settings.apply_library_to_live_drafts(PLAYER, GUILD)
    assert first == (f"https://discord.com/channels/{GUILD}/chan/msg-q1\n{COULD_NOT_CHECK}", None)
    assert second[0].startswith(f"https://discord.com/channels/{GUILD}/chan/msg-q2\n")


async def test_a_draft_that_started_before_the_reclaim_is_skipped(test_db, shelf, monkeypatch):
    import services.library_prompt as library_prompt
    await _lounge(stock={"Swamp": 4}); await _drafted(PLAYER, 3); await _queue([PLAYER])
    reclaim = AsyncMock()
    monkeypatch.setattr(library_prompt, "reclaim_player", reclaim)
    real = player_settings.live_drafts

    async def then_started(player_id, guild_id):
        drafts = await real(player_id, guild_id)
        await close("q1", teams_start_time=datetime.now())
        return drafts
    monkeypatch.setattr(player_settings, "live_drafts", then_started)
    await set_uses_library(PLAYER, GUILD, True)
    assert await player_settings.apply_library_to_live_drafts(PLAYER, GUILD) == []
    reclaim.assert_not_awaited()


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


# --- the usual week ---------------------------------------------------------

def _week_settings(**fields):
    return Settings(dms=True, hide_from_ladder=False, anonymous=False, **fields)


def _week_field(embed):
    return next((f for f in embed.fields if f.name == "Your usual week"), None)


async def test_settings_line_for_unset_and_empty_week():
    unset = _week_field(settings_embed("Lounge", _week_settings(scheduler=True, usual_week=None)))
    assert unset.value.startswith("Not set.")
    empty = _week_field(settings_embed("Lounge", _week_settings(scheduler=True, usual_week=[])))
    assert empty.value == "No usual times."
    two = _week_field(settings_embed(
        "Lounge", _week_settings(scheduler=True, usual_week=["a", "b"])))
    assert two.value == "a\nb"


async def test_a_long_week_is_cut_at_a_line_boundary():
    lines = [f"<t:{1700000000 + i}:F> to <t:{1700003600 + i}:t>" for i in range(60)]
    value = _week_field(settings_embed(
        "Lounge", _week_settings(scheduler=True, usual_week=lines))).value
    assert len(value) <= 1024
    assert all(part in lines for part in value.split("\n"))


async def test_week_field_absent_when_scheduler_off():
    e = settings_embed("Lounge", _week_settings(scheduler=False, usual_week=["a"]))
    assert _week_field(e) is None


async def test_week_button_only_when_enabled():
    on = SettingsView("p1", "g", "Lounge", _week_settings(scheduler=True))
    off = SettingsView("p1", "g", "Lounge", _week_settings(scheduler=False))
    assert "My usual week" in [getattr(c, "label", None) for c in on.children]
    assert "My usual week" not in [getattr(c, "label", None) for c in off.children]


async def test_week_button_replies_with_a_link_button(monkeypatch):
    issue = AsyncMock(return_value="https://draftbot.fyi/#tok")
    monkeypatch.setattr(player_settings_view, "issue_link", issue)
    view = SettingsView("p1", "g", "Lounge", _week_settings(scheduler=True))
    i = _interaction("p1")
    await view.week_button.callback(i)
    assert issue.await_args.args[:3] == ("p1", "g", "usual")
    kwargs = i.followup.send.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert i.followup.send.await_args.args[0] == (
        "Paint your usual week, then save. It shows here straight away. "
        "The link works for 7 days.")
    buttons = [c for c in kwargs["view"].children if isinstance(c, discord.ui.Button)]
    assert [(b.label, b.url) for b in buttons] == [("Open my week", "https://draftbot.fyi/#tok")]


async def test_load_settings_reads_the_week_when_enabled(test_db, monkeypatch):
    monkeypatch.setattr(player_settings, "scheduler_enabled", lambda g: True)
    monkeypatch.setattr(player_settings, "load_usual_week", AsyncMock(return_value=None))
    s = await load_settings("p1", "g")
    assert (s.scheduler, s.usual_week) == (True, None)


async def test_week_button_says_so_on_any_failure(monkeypatch):
    monkeypatch.setattr(player_settings_view, "issue_link",
                        AsyncMock(side_effect=RuntimeError("db")))
    view = SettingsView("p1", "g", "Lounge", _week_settings(scheduler=True))
    i = _interaction("p1")
    await view.week_button.callback(i)
    assert i.followup.send.await_args.args[0] == (
        "Couldn't make your link just now. Try again in a minute.")


async def test_an_unreadable_week_does_not_break_settings(test_db, monkeypatch):
    monkeypatch.setattr(player_settings, "scheduler_enabled", lambda g: True)
    monkeypatch.setattr(player_settings, "load_usual_week",
                        AsyncMock(side_effect=ValueError("bad cells")))
    s = await load_settings("p1", "g")
    assert (s.scheduler, s.usual_week) == (True, None)
