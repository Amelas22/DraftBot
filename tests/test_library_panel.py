"""The library panel posted in a draft's chat when the draft has library decks."""
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from services import library_panel
from services.library_panel import Borrower, borrow_embed, post_library_panel


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
