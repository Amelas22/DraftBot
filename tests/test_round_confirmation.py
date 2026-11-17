from datetime import date, datetime, timezone

import pytest

from database.db_session import db_session
from models.scheduler import PlayerSchedulerSetting
from services.round_confirmation import (
    add_confirmations,
    confirm_round,
    confirmation_doc,
    confirmed_rounds,
    set_trusts_usual,
    trusts_usual_of,
)

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_no_row_and_null_both_mean_on(test_db):
    async with db_session() as s:
        s.add(PlayerSchedulerSetting(user_id="b", trusts_usual=None))
    async with db_session() as s:
        assert await trusts_usual_of(s, ["a", "b"]) == {"a": True, "b": True}


@pytest.mark.asyncio
async def test_turning_the_switch_on_keeps_confirmations(test_db):
    await set_trusts_usual("u", False)
    await confirm_round("u", date(2026, 11, 16), NOW)
    await set_trusts_usual("u", True)
    await set_trusts_usual("u", False)
    async with db_session() as s:
        assert (await trusts_usual_of(s, ["u"])) == {"u": False}
        assert (await confirmed_rounds(s, ["u"], date(2026, 11, 16), date(2026, 11, 16)))["u"] == {date(2026, 11, 16)}


@pytest.mark.asyncio
async def test_confirmed_rounds_is_limited_to_the_range(test_db):
    async with db_session() as s:
        await add_confirmations(s, "u", [date(2026, 11, 9), date(2026, 11, 16), date(2026, 11, 23)], NOW, "button")
    async with db_session() as s:
        got = await confirmed_rounds(s, ["u", "v"], date(2026, 11, 16), date(2026, 11, 16))
    assert got == {"u": frozenset({date(2026, 11, 16)}), "v": frozenset()}


@pytest.mark.asyncio
async def test_confirming_twice_keeps_one_row_and_doc_is_ascending(test_db):
    await confirm_round("u", date(2026, 11, 23), NOW)
    await confirm_round("u", date(2026, 11, 16), NOW)
    await confirm_round("u", date(2026, 11, 16), NOW)
    doc = await confirmation_doc("u", date(2026, 11, 1), date(2026, 12, 1))
    assert doc["confirmed_rounds"] == ["2026-11-16", "2026-11-23"]


@pytest.mark.asyncio
async def test_calling_add_confirmations_twice_in_one_transaction_is_fine(test_db):
    async with db_session() as s:
        await add_confirmations(s, "u", [date(2026, 11, 16)], NOW, "override")
        await add_confirmations(s, "u", [date(2026, 11, 16), date(2026, 11, 23)], NOW, "button")
    doc = await confirmation_doc("u", date(2026, 11, 1), date(2026, 12, 1))
    assert doc["confirmed_rounds"] == ["2026-11-16", "2026-11-23"]


@pytest.mark.asyncio
async def test_set_trusts_usual_upserts(test_db):
    await set_trusts_usual("u", False)
    await set_trusts_usual("u", True)
    await set_trusts_usual("u", False)
    async with db_session() as s:
        assert (await trusts_usual_of(s, ["u"]))["u"] is False
