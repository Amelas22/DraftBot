"""The library panel in a draft's chat, driven through sync_panel: posted when
the chat opens, switched to returns once the draft is decided, closed once
every deck is back, and recovered by the watchdog's reconcile."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import AsyncSessionLocal
from database.message_management import Message
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from services import library_panel, library_panel_state
from services.library_panel import result_line, sync_panel


@pytest.fixture(autouse=True)
def fresh_panel_memory():
    library_panel._shown.clear()
    yield
    library_panel._shown.clear()


def _text(embed):
    return "\n".join([embed.title or "", embed.description or ""]
                     + [f"{f.name}\n{f.value}" for f in embed.fields])


def _line(embed, name):
    return next(line for line in _text(embed).splitlines() if name in line)


def _field(embed, name):
    return next((f.value for f in embed.fields if f.name == name), "")


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
    return await DraftSession.get_by_session_id("s1")


def _message(message_id):
    m = MagicMock()
    m.id = message_id
    m.content = ""
    m.pinned = False
    m.edit = AsyncMock()
    m.delete = AsyncMock()
    m.pin = AsyncMock()
    m.unpin = AsyncMock()
    return m


def _bot():
    sent = _message(9001)
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock(return_value=sent)
    channel.fetch_message = AsyncMock(return_value=sent)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    return bot, channel, sent


@pytest.fixture
def reserved(monkeypatch):
    async def users(draft, library_id=None):
        return {"3"}
    monkeypatch.setattr(library_panel_state, "library_users", users)


@pytest.fixture
def undecided(monkeypatch):
    async def state(session_id):
        return SimpleNamespace(settled=False)
    monkeypatch.setattr(library_panel, "draft_state", state)


# --- Posting it when the chat opens -------------------------------------------


@pytest.mark.asyncio
async def test_no_panel_without_library_involvement(test_db, monkeypatch):  # noqa: F811
    async def none(draft, library_id=None):
        return set()
    monkeypatch.setattr(library_panel_state, "library_users", none)
    await _seed({"1": "Ravenwood"})
    bot, channel, _ = _bot()
    await sync_panel(bot, "s1")
    channel.send.assert_not_awaited()
    assert (await _row()).library_panel_mode is None


@pytest.mark.asyncio
async def test_panel_is_posted_once_and_its_id_stored(test_db, reserved):  # noqa: F811
    await _seed({"1": "Ravenwood", "3": "Tomasz88"}, loans=[("1", "borrowed")])
    bot, channel, sent = _bot()
    await sync_panel(bot, "s1")
    await sync_panel(bot, "s1")
    assert channel.send.await_count == 1
    assert channel.send.await_args.kwargs["view"] is not None
    row = await _row()
    assert row.library_panel_message_id == str(sent.id)
    assert row.library_panel_mode == "borrow"
    # Nothing changed between the two, so the second drew nothing.
    sent.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_panel_without_a_draft_chat(test_db, reserved):  # noqa: F811
    await _seed({"3": "Tomasz88"}, chat=None)
    bot, channel, _ = _bot()
    await sync_panel(bot, "s1")
    channel.send.assert_not_awaited()
    bot.get_channel.assert_not_called()
    assert (await _row()).library_panel_mode is None


@pytest.mark.asyncio
async def test_a_failed_post_leaves_no_panel(test_db, reserved):  # noqa: F811
    await _seed({"3": "Tomasz88"})
    bot, channel, _ = _bot()
    channel.send = AsyncMock(side_effect=RuntimeError("403"))
    with pytest.raises(RuntimeError):
        await sync_panel(bot, "s1")
    row = await _row()
    assert row.library_panel_mode is None and row.library_panel_message_id is None


@pytest.mark.asyncio
async def test_two_concurrent_syncs_post_one_panel(test_db, reserved, undecided):  # noqa: F811
    await _seed({"3": "Tomasz88"})
    bot, channel, sent = _bot()

    async def slow_send(**kwargs):
        await asyncio.sleep(0.05)
        return sent
    channel.send = AsyncMock(side_effect=slow_send)
    await asyncio.gather(sync_panel(bot, "s1"), sync_panel(bot, "s1"))
    assert channel.send.await_count == 1
    assert (await _row()).library_panel_message_id == str(sent.id)


@pytest.mark.asyncio
async def test_a_nudge_syncs_and_never_raises(monkeypatch):
    calls = []

    async def boom(bot, session_id):
        calls.append(session_id)
        raise RuntimeError("discord down")
    monkeypatch.setattr(library_panel, "sync_panel", boom)
    await library_panel.nudge(MagicMock(), "s1")
    assert calls == ["s1"]


# --- Return mode, once the draft is decided ------------------------------------


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


@pytest.fixture
def settled(monkeypatch):
    async def state(session_id):
        return SimpleNamespace(settled=True)
    monkeypatch.setattr(library_panel, "draft_state", state)
    _wins(monkeypatch, 5, 3)


async def _in_mode(mode, loans, panel_id="777", sign_ups=None):
    await _seed(sign_ups or {"1": "Ravenwood", "3": "Tomasz88"}, loans=loans)
    await library_panel._write("s1", library_panel_mode=mode,
                               library_panel_message_id=panel_id)


def _bot_with_old_panel(fetch_error=None):
    """A chat whose stored panel is `old` (777); whatever is sent is `sent` (9001)."""
    bot, channel, sent = _bot()
    old = _message(777)
    messages = {777: old, 9001: sent}

    async def fetch(message_id):
        if fetch_error is not None and message_id == 777:
            raise fetch_error
        return messages[message_id]
    channel.fetch_message = AsyncMock(side_effect=fetch)
    return bot, channel, sent, old


def _gone():
    return discord.NotFound(MagicMock(status=404), "Unknown")


@pytest.mark.asyncio
async def test_switch_posts_return_panel_and_deletes_borrow_panel(
        test_db, reserved, settled):  # noqa: F811
    await _in_mode("borrow", [("1", "borrowed")])
    bot, channel, sent, old = _bot_with_old_panel()
    await sync_panel(bot, "s1")
    row = await _row()
    assert row.library_panel_mode == "return"
    assert row.library_panel_message_id == str(sent.id)
    channel.send.assert_awaited_once()
    assert channel.send.await_args.kwargs["view"] is not None
    channel.fetch_message.assert_awaited_once_with(777)
    old.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_switch_twice_posts_once(test_db, reserved, settled):  # noqa: F811
    await _in_mode("borrow", [("1", "borrowed")])
    bot, channel, sent, _ = _bot_with_old_panel()

    async def slow_send(**kwargs):
        await asyncio.sleep(0.05)
        return sent
    channel.send = AsyncMock(side_effect=slow_send)
    await asyncio.gather(sync_panel(bot, "s1"), sync_panel(bot, "s1"))
    assert channel.send.await_count == 1


@pytest.mark.asyncio
async def test_switch_with_every_deck_back_closes_in_place(
        test_db, reserved, settled):  # noqa: F811
    await _in_mode("borrow", [("1", "returned")])
    bot, channel, _, old = _bot_with_old_panel()
    await sync_panel(bot, "s1")
    assert (await _row()).library_panel_mode == "closed"
    channel.send.assert_not_awaited()
    old.edit.assert_awaited_once()
    assert old.edit.await_args.kwargs["view"] is None
    assert "closed" in old.edit.await_args.kwargs["embed"].description


@pytest.mark.asyncio
async def test_switch_survives_a_missing_old_panel(test_db, reserved, settled):  # noqa: F811
    await _in_mode("borrow", [("1", "borrowed")])
    bot, _, sent, _ = _bot_with_old_panel(fetch_error=_gone())
    await sync_panel(bot, "s1")
    row = await _row()
    assert row.library_panel_mode == "return"
    assert row.library_panel_message_id == str(sent.id)


@pytest.mark.asyncio
async def test_an_undecided_draft_keeps_its_borrow_panel(
        test_db, reserved, undecided):  # noqa: F811
    await _in_mode("borrow", [("1", "borrowed")])
    bot, channel, _, old = _bot_with_old_panel()
    await sync_panel(bot, "s1")
    assert (await _row()).library_panel_mode == "borrow"
    channel.send.assert_not_awaited()
    old.delete.assert_not_awaited()
    assert "collected" in _line(old.edit.await_args.kwargs["embed"], "Ravenwood")


@pytest.mark.asyncio
async def test_failed_return_post_stays_in_borrow(test_db, reserved, settled):  # noqa: F811
    await _in_mode("borrow", [("1", "borrowed")])
    bot, channel, _, old = _bot_with_old_panel()
    channel.send = AsyncMock(side_effect=RuntimeError("403"))
    with pytest.raises(RuntimeError):
        await sync_panel(bot, "s1")
    row = await _row()
    assert row.library_panel_mode == "borrow" and row.library_panel_message_id == "777"
    old.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_victory_check_syncs_the_panel_on_its_way_out(
        test_db, monkeypatch):  # noqa: F811
    from utils import check_and_post_victory_or_draw
    calls = []

    async def sync(bot, session_id):
        calls.append(session_id)
    monkeypatch.setattr(library_panel, "sync_panel", sync)
    # No such draft: the check returns at its first exit, and the sync still runs.
    await check_and_post_victory_or_draw(MagicMock(), "nope")
    assert calls == ["nope"]


@pytest.mark.asyncio
async def test_a_failing_sync_does_not_break_the_victory_check(
        test_db, monkeypatch):  # noqa: F811
    from utils import check_and_post_victory_or_draw

    async def boom(bot, session_id):
        raise RuntimeError("discord down")
    monkeypatch.setattr(library_panel, "sync_panel", boom)
    await check_and_post_victory_or_draw(MagicMock(), "nope")


# --- Watchdog recovery -----------------------------------------------------------


async def _sticky_rows():
    from sqlalchemy import select

    async with AsyncSessionLocal() as s:
        return (await s.execute(select(Message).filter_by(is_sticky=True))).scalars().all()


@pytest.mark.asyncio
async def test_reconcile_switches_a_missed_decision(test_db, reserved, settled):  # noqa: F811
    await _in_mode("borrow", [("1", "borrowed")])
    bot, channel, sent, old = _bot_with_old_panel()
    await library_panel.reconcile_library_panels(bot)
    row = await _row()
    assert row.library_panel_mode == "return"
    assert row.library_panel_message_id == str(sent.id)
    channel.send.assert_awaited_once()
    old.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_switches_an_abandoned_draft(test_db, reserved, monkeypatch):  # noqa: F811
    _wins(monkeypatch, 5, 3)
    await _in_mode("borrow", [("1", "borrowed")])
    # The real draft_state: an abandoned draft counts as settled.
    await library_panel._write("s1", session_stage="abandoned")
    bot, channel, _, _ = _bot_with_old_panel()
    await library_panel.reconcile_library_panels(bot)
    assert (await _row()).library_panel_mode == "return"
    embed = channel.send.await_args.kwargs["embed"]
    assert "won" not in (embed.description or "")
    assert (embed.description or "").startswith("If you borrowed a deck")


@pytest.mark.asyncio
async def test_reconcile_reposts_a_deleted_return_panel(test_db, reserved, settled):  # noqa: F811
    await _in_mode("return", [("1", "borrowed")], sign_ups={"1": "Ravenwood"})
    bot, channel, sent, _ = _bot_with_old_panel(fetch_error=_gone())
    await library_panel.reconcile_library_panels(bot)
    row = await _row()
    assert row.library_panel_mode == "return"
    assert row.library_panel_message_id == str(sent.id)
    channel.send.assert_awaited_once()
    assert channel.send.await_args.kwargs["view"] is not None
    [sticky] = await _sticky_rows()
    assert sticky.message_id == str(sent.id)


@pytest.mark.asyncio
async def test_reconcile_refreshes_a_live_return_panel_in_place(
        test_db, reserved, settled):  # noqa: F811
    await _in_mode("return", [("1", "borrowed")], sign_ups={"1": "Ravenwood"})
    bot, channel, _, old = _bot_with_old_panel()
    await library_panel.reconcile_library_panels(bot)
    channel.send.assert_not_awaited()
    old.edit.assert_awaited_once()
    assert (await _row()).library_panel_message_id == "777"
    # The next tick finds it already right and leaves it be.
    await library_panel.reconcile_library_panels(bot)
    old.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_closes_a_deleted_return_panel_with_every_deck_back(
        test_db, reserved, settled):  # noqa: F811
    await _in_mode("return", [("1", "returned")], sign_ups={"1": "Ravenwood"})
    bot, channel, _, _ = _bot_with_old_panel(fetch_error=_gone())
    await library_panel.reconcile_library_panels(bot)
    assert (await _row()).library_panel_mode == "closed"
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_leaves_closed_panels_alone(test_db, reserved, settled):  # noqa: F811
    await _in_mode("closed", [("1", "borrowed")], sign_ups={"1": "Ravenwood"})
    bot, channel, _, old = _bot_with_old_panel()
    await library_panel.reconcile_library_panels(bot)
    bot.get_channel.assert_not_called()
    channel.send.assert_not_awaited()
    old.edit.assert_not_awaited()
    old.delete.assert_not_awaited()
    assert (await _row()).library_panel_mode == "closed"


@pytest.mark.asyncio
async def test_reconcile_switches_a_borrow_panel_whose_id_was_never_stored(
        test_db, reserved, settled):  # noqa: F811
    # A crash between sending the borrow panel and storing its id.
    await _in_mode("borrow", [("1", "borrowed")], panel_id=None,
                   sign_ups={"1": "Ravenwood"})
    bot, channel, sent, _ = _bot_with_old_panel()
    await library_panel.reconcile_library_panels(bot)
    row = await _row()
    assert row.library_panel_mode == "return"
    assert row.library_panel_message_id == str(sent.id)
    channel.send.assert_awaited_once()
    channel.fetch_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_does_not_repost_an_undecided_borrow_panel(
        test_db, reserved, undecided):  # noqa: F811
    await _in_mode("borrow", [("1", "borrowed")], panel_id=None,
                   sign_ups={"1": "Ravenwood"})
    bot, channel, _, _ = _bot_with_old_panel()
    await library_panel.reconcile_library_panels(bot)
    channel.send.assert_not_awaited()
    row = await _row()
    assert row.library_panel_mode == "borrow" and row.library_panel_message_id is None


@pytest.mark.asyncio
async def test_reconcile_closes_a_panel_whose_chat_is_gone(test_db, reserved, settled):  # noqa: F811
    await _in_mode("borrow", [("1", "borrowed")])
    bot, channel, _, _ = _bot_with_old_panel()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(side_effect=_gone())
    await library_panel.reconcile_library_panels(bot)
    assert (await _row()).library_panel_mode == "closed"
    await library_panel.reconcile_library_panels(bot)
    bot.fetch_channel.assert_awaited_once()
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_finds_a_chat_missing_from_the_cache(test_db, reserved, settled):  # noqa: F811
    await _in_mode("borrow", [("1", "borrowed")])
    bot, channel, sent, _ = _bot_with_old_panel()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(return_value=channel)
    await library_panel.reconcile_library_panels(bot)
    row = await _row()
    assert row.library_panel_mode == "return"
    assert row.library_panel_message_id == str(sent.id)


@pytest.mark.asyncio
async def test_one_failing_draft_does_not_stop_the_scan(test_db, monkeypatch):  # noqa: F811
    async with AsyncSessionLocal() as s:
        for sid, mode in (("a", "borrow"), ("b", "return"), ("c", "closed"), ("d", None)):
            s.add(DraftSession(session_id=sid, guild_id="g", draft_chat_channel="555",
                               library_panel_mode=mode))
        await s.commit()
    seen = []

    async def sync(bot, session_id):
        seen.append(session_id)
        if session_id == "a":
            raise RuntimeError("discord down")
    monkeypatch.setattr(library_panel, "sync_panel", sync)
    await library_panel.reconcile_library_panels(MagicMock())
    assert sorted(seen) == ["a", "b"]


@pytest.mark.asyncio
async def test_the_watchdog_reconciles_library_panels(test_db, monkeypatch):  # noqa: F811
    from services import card_lending_service as lending

    for name in ("settle_in_flight", "expire_stale_assignments"):
        monkeypatch.setattr(lending, name, AsyncMock(return_value={}))
    calls = []
    done = asyncio.Event()

    async def reconcile(bot):
        calls.append(bot)
        done.set()
    monkeypatch.setattr(library_panel, "reconcile_library_panels", reconcile)
    monkeypatch.setattr(lending, "_watchdog_running", False)
    bot = MagicMock()
    task = asyncio.create_task(lending.lending_jobs_watchdog(bot, interval_s=0.01))
    try:
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls[0] is bot


# --- A deck still on offer keeps the panel open ----------------------------------


async def _loan_now(borrower, state):
    from sqlalchemy import update

    async with AsyncSessionLocal() as s:
        await s.execute(update(CardLoan).where(CardLoan.borrower_id == borrower)
                        .values(state=state))
        await s.commit()


@pytest.mark.asyncio
async def test_a_deck_on_offer_keeps_the_borrow_panel_open_until_it_lapses(
        test_db, reserved, settled):  # noqa: F811
    await _in_mode("borrow", [("1", "assigned")])
    bot, channel, _, old = _bot_with_old_panel()
    await sync_panel(bot, "s1")
    assert (await _row()).library_panel_mode == "borrow"
    channel.send.assert_not_awaited()
    assert old.edit.await_args.kwargs["view"] is not None

    await _loan_now("1", "expired")
    await library_panel.reconcile_library_panels(bot)
    assert (await _row()).library_panel_mode == "closed"
    assert old.edit.await_args.kwargs["view"] is None
    assert "back on the shelf" in _line(old.edit.await_args.kwargs["embed"], "Ravenwood")


@pytest.mark.asyncio
async def test_a_deck_collected_after_the_decision_gets_a_return_panel(
        test_db, reserved, settled):  # noqa: F811
    await _in_mode("borrow", [("1", "assigned")])
    bot, channel, sent, _ = _bot_with_old_panel()
    await sync_panel(bot, "s1")

    await _loan_now("1", "borrowed")
    await library_panel.reconcile_library_panels(bot)
    row = await _row()
    assert row.library_panel_mode == "return"
    assert row.library_panel_message_id == str(sent.id)
    embed = channel.send.await_args.kwargs["embed"]
    assert "Ravenwood" in _field(embed, "Still out")


@pytest.mark.asyncio
async def test_a_deck_on_offer_keeps_the_return_panel_open_until_it_lapses(
        test_db, reserved, settled):  # noqa: F811
    await _in_mode("return", [("1", "returned"), ("2", "assigned")],
                   sign_ups={"1": "Ravenwood", "2": "kestrel_mtg"})
    bot, _, _, old = _bot_with_old_panel()
    await sync_panel(bot, "s1")
    assert (await _row()).library_panel_mode == "return"
    assert old.edit.await_args.kwargs["view"] is not None

    await _loan_now("2", "expired")
    await sync_panel(bot, "s1")
    assert (await _row()).library_panel_mode == "closed"
    assert old.edit.await_args.kwargs["view"] is None
