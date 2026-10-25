"""In return mode the library panel is sticky: it is reposted at the bottom of the
draft chat, the draft's panel pointer follows it, and loan changes re-render the
live copy. Once every deck is back it closes and stops moving."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import select

import bot_registry
from conftest import test_db  # noqa: F401  (fixture)
from database.db_session import AsyncSessionLocal
from database.message_management import (
    Message, StickyUpdateResult, handle_sticky_message_update)
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from services import library_panel


@pytest.fixture
def decided(monkeypatch):
    import utils

    async def users(draft, library_id=None):
        return set()

    async def wins(session_id):
        return 5, 3

    async def state(session_id):
        return SimpleNamespace(settled=True)
    monkeypatch.setattr(library_panel, "library_users", users)
    monkeypatch.setattr(library_panel, "draft_state", state)
    monkeypatch.setattr(utils, "calculate_team_wins", wins)


async def _seed(loan_state, mode="return", panel_id="900", sticky_count=None):
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="s1", guild_id="g1", sign_ups={"1": "Ravenwood"},
                           draft_chat_channel="777", session_type="random",
                           session_stage="pairings", library_panel_mode=mode,
                           library_panel_message_id=panel_id))
        s.add(CardLoan(guild_id="g1", library_id="lib", borrower_id="1",
                       cards=[{"name": "Swamp", "qty": 45}], state=loan_state,
                       source="draft:s1"))
        if sticky_count is not None:
            s.add(Message(guild_id="g1", channel_id="777", message_id=panel_id,
                          content="", is_sticky=True, message_count=sticky_count,
                          view_metadata={"view_type": "library_panel",
                                         "draft_session_id": "s1"},
                          last_activity=0.0, last_update_time=0.0))
        await s.commit()


async def _draft():
    return await DraftSession.get_by_session_id("s1")


async def _sticky_rows():
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(Message).filter_by(is_sticky=True))).scalars().all()


def _message(message_id, channel):
    m = MagicMock()
    m.id = message_id
    m.channel = channel
    m.content = ""
    m.pinned = False
    m.pin = AsyncMock()
    m.delete = AsyncMock()
    m.edit = AsyncMock()
    return m


def _bot(*message_ids, send_id=901):
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 777
    messages = {mid: _message(mid, channel) for mid in message_ids}
    sent = _message(send_id, channel)
    messages[send_id] = sent
    channel.send = AsyncMock(return_value=sent)
    channel.fetch_message = AsyncMock(side_effect=lambda mid: messages[mid])
    bot = MagicMock()
    bot.fetch_channel = AsyncMock(return_value=channel)  # the sticky manager
    bot.get_channel = MagicMock(return_value=channel)  # the panel itself
    return bot, channel, messages


@pytest.mark.asyncio
async def test_the_return_panel_is_posted_sticky(test_db, decided):  # noqa: F811
    await _seed("borrowed", mode="borrow", panel_id="900")
    bot, _, _ = _bot(900, send_id=901)

    await library_panel.switch_to_returns(bot, "s1")

    [sticky] = await _sticky_rows()
    assert sticky.message_id == "901" and sticky.channel_id == "777"
    assert sticky.view_metadata == {"view_type": "library_panel", "draft_session_id": "s1"}
    assert (await _draft()).library_panel_message_id == "901"


@pytest.mark.asyncio
async def test_sticky_repost_repoints_the_panel(test_db, decided):  # noqa: F811
    await _seed("borrowed", sticky_count=10)
    bot, channel, messages = _bot(900, send_id=901)

    async with AsyncSessionLocal() as s:
        sticky = (await s.execute(select(Message))).scalars().one()
        assert await handle_sticky_message_update(sticky, bot, s) == StickyUpdateResult.SUCCESS

    assert (await _draft()).library_panel_message_id == "901"
    sent = channel.send.await_args.kwargs
    assert sent["embed"].title == library_panel.TITLE and sent["view"] is not None
    messages[900].delete.assert_awaited_once()
    # After the commit, the new copy is redrawn from committed state.
    messages[901].edit.assert_awaited()


@pytest.mark.asyncio
async def test_the_sticky_ends_once_the_panel_is_closed(test_db, decided):  # noqa: F811
    await _seed("returned", mode="closed", sticky_count=10)
    bot, channel, _ = _bot(900)

    async with AsyncSessionLocal() as s:
        sticky = (await s.execute(select(Message))).scalars().one()
        assert await handle_sticky_message_update(sticky, bot, s) == StickyUpdateResult.CLEANED_UP

    channel.send.assert_not_awaited()
    assert await _sticky_rows() == []


@pytest.mark.asyncio
async def test_sticky_ends_when_every_deck_is_back(test_db, decided):  # noqa: F811
    await _seed("returned", sticky_count=0)
    bot, channel, messages = _bot(900)

    await library_panel.refresh_library_panel(bot, "s1")

    assert (await _draft()).library_panel_mode == "closed"
    edit = messages[900].edit.await_args.kwargs
    assert edit["view"] is None
    assert "All decks are back. Thanks!" in edit["embed"].description
    assert await _sticky_rows() == []
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refresh_redraws_a_return_panel_with_decks_out(test_db, decided):  # noqa: F811
    await _seed("borrowed", sticky_count=0)
    bot, _, messages = _bot(900)

    await library_panel.refresh_library_panel(bot, "s1")

    assert (await _draft()).library_panel_mode == "return"
    edit = messages[900].edit.await_args.kwargs
    assert edit["view"] is not None
    assert any(f.name == "Still out" for f in edit["embed"].fields)
    assert len(await _sticky_rows()) == 1


@pytest.mark.asyncio
async def test_a_refresh_redraws_a_borrow_panel(test_db, decided):  # noqa: F811
    await _seed("out_pending", mode="borrow")
    bot, _, messages = _bot(900)

    await library_panel.refresh_library_panel(bot, "s1")

    edit = messages[900].edit.await_args.kwargs
    assert "trade on its way" in edit["embed"].description
    assert edit["view"] is not None


@pytest.mark.asyncio
async def test_a_refresh_leaves_a_closed_panel_alone(test_db, decided):  # noqa: F811
    await _seed("returned", mode="closed")
    bot, _, messages = _bot(900)

    await library_panel.refresh_library_panel(bot, "s1")

    messages[900].edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refresh_survives_a_deleted_panel(test_db, decided):  # noqa: F811
    await _seed("borrowed", sticky_count=0)
    bot, channel, _ = _bot()
    channel.fetch_message = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "gone"))

    await library_panel.refresh_library_panel(bot, "s1")

    assert (await _draft()).library_panel_mode == "return"


@pytest.fixture
def spy_refresh(monkeypatch):
    calls = []

    async def refresh(bot, session_id):
        calls.append((bot, session_id))
    monkeypatch.setattr(library_panel, "refresh_library_panel", refresh)
    return calls


@pytest.mark.asyncio
async def test_a_settled_return_refreshes_the_panel(monkeypatch, spy_refresh):
    bot = MagicMock()
    monkeypatch.setattr(bot_registry, "get_bot", lambda: bot)

    library_panel.loan_changed(SimpleNamespace(source="draft:s1"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert spy_refresh == [(bot, "s1")]


@pytest.mark.asyncio
async def test_a_loan_from_no_draft_refreshes_nothing(monkeypatch, spy_refresh):
    monkeypatch.setattr(bot_registry, "get_bot", lambda: MagicMock())

    library_panel.loan_changed(SimpleNamespace(source="fixture:week1"))
    await asyncio.sleep(0)

    assert spy_refresh == []


@pytest.mark.asyncio
async def test_no_bot_refreshes_nothing(monkeypatch, spy_refresh):
    monkeypatch.setattr(bot_registry, "get_bot", lambda: None)

    library_panel.loan_changed(SimpleNamespace(source="draft:s1"))
    await asyncio.sleep(0)

    assert spy_refresh == []


@pytest.mark.asyncio
async def test_a_failing_refresh_never_reaches_the_lending_code(monkeypatch):
    async def boom(bot, session_id):
        raise RuntimeError("discord down")
    monkeypatch.setattr(library_panel, "refresh_library_panel", boom)
    monkeypatch.setattr(bot_registry, "get_bot", lambda: MagicMock())

    library_panel.loan_changed(SimpleNamespace(source="draft:s1"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_parking_a_loan_tells_the_panel(test_db, monkeypatch):  # noqa: F811
    from services import card_lending_service

    seen = []
    monkeypatch.setattr(library_panel, "loan_changed", seen.append)
    async with AsyncSessionLocal() as s:
        loan = CardLoan(guild_id="g1", library_id="lib", borrower_id="1",
                        cards=[], state="assigned", source="draft:s1")
        s.add(loan)
        await s.commit()

    await card_lending_service._park_for_review(loan)

    assert seen == [loan]


@pytest.mark.asyncio
async def test_retracting_offers_tells_the_panel_per_loan(test_db, monkeypatch):  # noqa: F811
    from services import card_lending_service

    seen = []
    monkeypatch.setattr(library_panel, "loan_changed", seen.append)
    async with AsyncSessionLocal() as s:
        loans = [CardLoan(guild_id="g1", library_id="lib", borrower_id=b, cards=[],
                          state="assigned", source="draft:s1") for b in ("1", "2")]
        s.add_all(loans)
        await s.commit()
        await card_lending_service._retract(s, loans)

    assert seen == loans
