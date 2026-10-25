"""The library panel posted in a draft's chat when the draft has library decks."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from services import library_panel
from services.library_panel import (Borrower, borrow_embed, post_library_panel,
                                    result_line, return_embed, switch_to_returns)


def _text(embed):
    return "\n".join([embed.title or "", embed.description or ""]
                     + [f"{f.name}\n{f.value}" for f in embed.fields])


def _line(embed, name):
    return next(line for line in _text(embed).splitlines() if name in line)


def test_borrow_embed_lists_each_borrower_by_state():
    embed = borrow_embed([
        Borrower("1", "Ravenwood", "borrowed", []),
        Borrower("2", "kestrel_mtg", "assigned", []),
        Borrower("3", "Tomasz88", "out_pending", []),
        Borrower("4", "Ali", None, None),
    ])
    assert embed.title == "🏛️ Library decks"
    assert "collected" in _line(embed, "Ravenwood")
    assert "ready, not collected" in _line(embed, "kestrel_mtg")
    assert "trade on its way" in _line(embed, "Tomasz88")
    assert "being prepared" in _line(embed, "Ali")


def test_borrow_embed_closed_marks_uncollected_back_on_shelf():
    embed = borrow_embed([Borrower("1", "Ravenwood", "expired", [])], closed=True)
    assert "not collected, back on the shelf" in _line(embed, "Ravenwood")


async def _seed(sign_ups, chat="555", loans=()):
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="s1", guild_id="g", sign_ups=sign_ups,
                           draft_chat_channel=chat))
        for borrower, state in loans:
            s.add(CardLoan(guild_id="g", library_id="lib", borrower_id=borrower,
                           cards=[{"name": "Swamp", "qty": 4}], state=state,
                           source="draft:s1"))
        await s.commit()


async def _row():
    async with AsyncSessionLocal() as s:
        return await DraftSession.get_by_session_id("s1")


def _bot():
    sent = MagicMock()
    sent.id = 9001
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(return_value=sent)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    return bot, channel, sent


@pytest.fixture
def reserved(monkeypatch):
    async def users(draft, library_id=None):
        return {"3"}
    monkeypatch.setattr(library_panel, "library_users", users)


@pytest.mark.asyncio
async def test_no_panel_without_library_involvement(test_db, monkeypatch):
    async def none(draft, library_id=None):
        return set()
    monkeypatch.setattr(library_panel, "library_users", none)
    await _seed({"1": "Ravenwood"})
    bot, channel, _ = _bot()
    await post_library_panel(bot, "s1")
    channel.send.assert_not_awaited()
    assert (await _row()).library_panel_mode is None


@pytest.mark.asyncio
async def test_panel_is_posted_once_and_its_id_stored(test_db, reserved):
    await _seed({"1": "Ravenwood", "3": "Tomasz88"}, loans=[("1", "borrowed")])
    bot, channel, sent = _bot()
    await post_library_panel(bot, "s1")
    await post_library_panel(bot, "s1")
    assert channel.send.await_count == 1
    row = await _row()
    assert row.library_panel_message_id == str(sent.id)
    assert row.library_panel_mode == "borrow"


@pytest.mark.asyncio
async def test_borrowers_are_the_reserved_plus_loaned_newest_loan_by_name(test_db, reserved):
    await _seed({"1": "Ravenwood", "3": "Tomasz88"},
                loans=[("1", "returned"), ("1", "borrowed")])
    got = await library_panel.panel_borrowers("s1")
    assert [(b.name, b.state) for b in got] == [("Ravenwood", "borrowed"),
                                                ("Tomasz88", None)]


@pytest.mark.asyncio
async def test_failed_send_resets_the_mode(test_db, reserved):
    await _seed({"3": "Tomasz88"})
    bot, channel, _ = _bot()
    channel.send = AsyncMock(side_effect=RuntimeError("403"))
    with pytest.raises(RuntimeError):
        await post_library_panel(bot, "s1")
    row = await _row()
    assert row.library_panel_mode is None and row.library_panel_message_id is None


# --- Return mode, once the draft is decided ---------------------------------


def _wins(monkeypatch, a, b):
    import utils

    async def wins(session_id):
        return a, b
    monkeypatch.setattr(utils, "calculate_team_wins", wins)


def _draft(**kw):
    base = dict(session_id="s1", session_type="random", session_stage="pairings",
                team_a_name=None, team_b_name=None)
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.asyncio
async def test_result_line_names_the_winner(monkeypatch):
    _wins(monkeypatch, 5, 3)
    line = await result_line(_draft(team_a_name="Red"))
    assert line is not None and "won 5–3" in line and line.startswith("Red")


@pytest.mark.asyncio
async def test_result_line_says_draw(monkeypatch):
    _wins(monkeypatch, 4, 4)
    line = await result_line(_draft())
    assert line is not None and line.startswith("Draw")


@pytest.mark.asyncio
async def test_result_line_is_none_for_swiss_and_abandoned(monkeypatch):
    _wins(monkeypatch, 5, 3)
    assert await result_line(_draft(session_type="swiss")) is None
    assert await result_line(_draft(session_stage="abandoned")) is None


def _field(embed, name):
    return next((f.value for f in embed.fields if f.name == name), "")


def test_return_embed_names_who_still_holds_a_deck():
    cards = [{"name": "Swamp", "qty": 45}]
    embed = return_embed([
        Borrower("1", "Ravenwood", "borrowed", cards),
        Borrower("2", "kestrel_mtg", "returned", cards),
        Borrower("3", "Tomasz88", "expired", cards),
    ], "Red won 5–3")
    assert embed.title == "🏛️ Library decks"
    assert "Red won 5–3" in (embed.description or "")
    assert "Ravenwood" in _field(embed, "Still out")
    assert "45" in _field(embed, "Still out")
    assert "kestrel_mtg" in _field(embed, "Back")
    assert "Tomasz88" not in _text(embed)
    assert embed.footer.text == "Move the cards into your trade binder first"


@pytest.fixture
def settled(monkeypatch):
    async def state(session_id):
        return SimpleNamespace(settled=True)
    monkeypatch.setattr(library_panel, "draft_state", state)
    _wins(monkeypatch, 5, 3)


async def _in_borrow_mode(loans):
    await _seed({"1": "Ravenwood", "3": "Tomasz88"}, loans=loans)
    await library_panel._set_mode("s1", library_panel_mode="borrow",
                                  library_panel_message_id="777")


def _bot_with_old_panel(fetch_error=None):
    bot, channel, sent = _bot()
    old = MagicMock()
    old.delete = AsyncMock()
    old.edit = AsyncMock()
    channel.fetch_message = AsyncMock(side_effect=fetch_error, return_value=old)
    return bot, channel, sent, old


@pytest.mark.asyncio
async def test_switch_posts_return_panel_and_deletes_borrow_panel(test_db, reserved, settled):
    await _in_borrow_mode([("1", "borrowed")])
    bot, channel, sent, old = _bot_with_old_panel()
    await switch_to_returns(bot, "s1")
    row = await _row()
    assert row.library_panel_mode == "return"
    assert row.library_panel_message_id == str(sent.id)
    channel.send.assert_awaited_once()
    assert channel.send.await_args.kwargs["view"] is not None
    channel.fetch_message.assert_awaited_once_with(777)
    old.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_switch_twice_posts_once(test_db, reserved, settled):
    await _in_borrow_mode([("1", "borrowed")])
    bot, channel, _, _ = _bot_with_old_panel()
    await asyncio.gather(switch_to_returns(bot, "s1"), switch_to_returns(bot, "s1"))
    assert channel.send.await_count == 1


@pytest.mark.asyncio
async def test_switch_with_every_deck_back_closes_in_place(test_db, reserved, settled):
    await _in_borrow_mode([("1", "returned")])
    bot, channel, _, old = _bot_with_old_panel()
    await switch_to_returns(bot, "s1")
    assert (await _row()).library_panel_mode == "closed"
    channel.send.assert_not_awaited()
    old.edit.assert_awaited_once()
    assert old.edit.await_args.kwargs["view"] is None
    assert "closed" in old.edit.await_args.kwargs["embed"].description


@pytest.mark.asyncio
async def test_switch_survives_a_missing_old_panel(test_db, reserved, settled):
    await _in_borrow_mode([("1", "borrowed")])
    gone = discord.NotFound(MagicMock(status=404), "Unknown Message")
    bot, channel, sent, _ = _bot_with_old_panel(fetch_error=gone)
    await switch_to_returns(bot, "s1")
    row = await _row()
    assert row.library_panel_mode == "return"
    assert row.library_panel_message_id == str(sent.id)


@pytest.mark.asyncio
async def test_switch_ignores_an_undecided_draft(test_db, reserved, monkeypatch):
    async def state(session_id):
        return SimpleNamespace(settled=False)
    monkeypatch.setattr(library_panel, "draft_state", state)
    await _in_borrow_mode([("1", "borrowed")])
    bot, channel, _, old = _bot_with_old_panel()
    await switch_to_returns(bot, "s1")
    assert (await _row()).library_panel_mode == "borrow"
    channel.send.assert_not_awaited()
    old.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_return_post_goes_back_to_borrow(test_db, reserved, settled):
    await _in_borrow_mode([("1", "borrowed")])
    bot, channel, _, old = _bot_with_old_panel()
    channel.send = AsyncMock(side_effect=RuntimeError("403"))
    with pytest.raises(RuntimeError):
        await switch_to_returns(bot, "s1")
    row = await _row()
    assert row.library_panel_mode == "borrow" and row.library_panel_message_id == "777"
    old.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_victory_check_switches_the_panel_on_its_way_out(test_db, monkeypatch):
    from utils import check_and_post_victory_or_draw
    calls = []

    async def switch(bot, session_id):
        calls.append(session_id)
    monkeypatch.setattr(library_panel, "switch_to_returns", switch)
    # No such draft: the check returns at its first exit, and the switch still runs.
    await check_and_post_victory_or_draw(MagicMock(), "nope")
    assert calls == ["nope"]


@pytest.mark.asyncio
async def test_a_failing_switch_does_not_break_the_victory_check(test_db, monkeypatch):
    from utils import check_and_post_victory_or_draw

    async def boom(bot, session_id):
        raise RuntimeError("discord down")
    monkeypatch.setattr(library_panel, "switch_to_returns", boom)
    await check_and_post_victory_or_draw(MagicMock(), "nope")
