"""Every server reservation is run past the library ledger.

In shadow mode (the default) the ledger never changes the answer a queue gets:
it logs what it would have refused or allowed, and books a hold for each
reservation, so a week of real sign-ups can be audited before it decides
anything. In enforce mode a ledger refusal turns a reservation into "short".
A ledger that fails never changes the answer.
"""
import logging
from datetime import datetime

import pytest
from loguru import logger
from sqlalchemy import select

import config
import services.library_ledger_store as store
import services.library_reservation as resv
from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from models.library_hold import LibraryHold
from test_library_reservation import (  # noqa: F401  (lounge is a fixture)
    ANN, BEN, CUBE, GUILD, LIB, _borrower, _queue, _row, _set, lounge)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def logs(caplog):
    """loguru output, captured through caplog."""
    handle = logger.add(caplog.handler, format="{message}", level="INFO")
    caplog.set_level(logging.INFO)
    yield caplog
    logger.remove(handle)


@pytest.fixture
def mode(monkeypatch):
    """Set this guild's library_ledger_mode."""
    def set_mode(value):
        monkeypatch.setattr(config, "get_config",
                            lambda _gid: {"library_ledger_mode": value})
    set_mode("shadow")
    return set_mode


async def _holds(sid=None):
    async with AsyncSessionLocal() as s:
        q = select(LibraryHold)
        if sid is not None:
            q = q.where(LibraryHold.draft_session_id == sid)
        return list((await s.scalars(q)).all())


async def _another_draft_holds_the_copy():
    """A live server hold the reservation check doesn't know about: its draft
    holds a reservation, but in a server not bound to this library."""
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="elsewhere", guild_id="unbound",
                           cube=CUBE, library_reserved_at=datetime.now()))
        await s.commit()
    async with resv._RESERVE_LOCK:
        await store.book_server_hold(LIB, CUBE, "elsewhere", datetime.now())


async def test_shadow_logs_a_refusal_but_still_reserves(lounge, logs, mode):
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await _another_draft_holds_the_copy()

    assert (await resv.reserve("q1")).status == "reserved"

    assert "library ledger: would refuse q1" in logs.text
    assert [h.released_at for h in await _holds("q1")] == [None]


async def test_enforce_turns_a_refusal_into_short(lounge, logs, mode):
    mode("enforce")
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await _another_draft_holds_the_copy()

    assert (await resv.reserve("q1")).status == "short"

    assert "library ledger: would refuse q1" in logs.text
    assert await _holds("q1") == []
    assert (await _row("q1")).library_reserved_at is None


async def test_a_reserve_books_a_hold_and_the_release_ends_it(lounge, mode):
    await _borrower(ANN)
    await _queue("q1", [ANN])

    assert (await resv.reserve("q1")).status == "reserved"
    assert [h.released_at for h in await _holds("q1")] == [None]

    await resv.release_player("q1", ANN)        # still_reserved writes the release

    assert (await _row("q1")).library_released_at is not None
    holds = await _holds("q1")
    assert len(holds) == 1 and holds[0].released_at is not None


async def test_rejoin_keeps_one_live_hold(lounge, mode):
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await resv.reserve("q1")
    await resv.release_player("q1", ANN)

    assert (await resv.agree_player("q1", ANN)).status == "reserved"

    holds = await _holds("q1")
    assert sum(h.released_at is None for h in holds) == 1
    assert sum(h.released_at is not None for h in holds) == 1


async def test_a_failing_ledger_never_changes_the_answer(
        lounge, logs, mode, monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("ledger down")
    monkeypatch.setattr(store, "server_verdict", broken)
    monkeypatch.setattr(store, "book_server_hold", broken)
    await _borrower(ANN, BEN)
    await _queue("q1", [ANN])
    await _queue("q2", [BEN])

    assert (await resv.reserve("q1")).status == "reserved"
    answer = await resv.reserve("q2")
    assert answer.status == "in_use" and answer.by.session_id == "q1"

    assert "library ledger: verdict failed for q1" in logs.text
    assert "ledger down" in logs.text


async def test_a_failing_release_never_breaks_the_release(
        lounge, logs, mode, monkeypatch):
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await resv.reserve("q1")

    async def broken(*_args, **_kwargs):
        raise RuntimeError("ledger down")
    monkeypatch.setattr(store, "release_server_hold", broken)
    await _set("q1", library_released=[ANN])

    assert not await resv.still_reserved(await _row("q1"))
    assert (await _row("q1")).library_released_at is not None
    assert "ledger down" in logs.text


async def test_shadow_logs_what_it_would_allow(lounge, logs, mode, monkeypatch):
    """Today's check says the copy is in use; a ledger with room says yes."""
    await _borrower(ANN, BEN)
    await _queue("q1", [ANN])
    await _queue("q2", [BEN])
    await resv.reserve("q1")
    await store.release_server_hold("q1", datetime.now())   # ledger forgets q1

    answer = await resv.reserve("q2")

    assert answer.status == "in_use" and answer.by.session_id == "q1"
    assert "library ledger: would allow q2" in logs.text
    assert await _holds("q2") == []


async def test_an_unreadable_cube_for_the_ledger_is_logged(
        lounge, logs, mode, monkeypatch):
    async def unknown(*_args, **_kwargs):
        return None
    monkeypatch.setattr(store, "server_verdict", unknown)
    await _borrower(ANN)
    await _queue("q1", [ANN])

    assert (await resv.reserve("q1")).status == "reserved"
    assert "library ledger: unknown for q1" in logs.text


async def test_an_unknown_mode_reads_as_shadow(mode, logs):
    mode("loud")
    assert resv.ledger_mode(GUILD) == "shadow"
    assert "library_ledger_mode" in logs.text
    mode("enforce")
    assert resv.ledger_mode(GUILD) == "enforce"


async def test_a_release_noticed_by_todays_check_frees_the_ledger_too(
        lounge, logs, mode):
    """The release of a queue nobody needs is written down while today's check
    reads the reservations; the ledger is asked after, so it agrees."""
    await _borrower(ANN, BEN)
    await _queue("q1", [ANN])
    await _queue("q2", [BEN])
    await resv.reserve("q1")
    await _set("q1", sign_ups={"9999": "someone else"})

    assert (await resv.reserve("q2")).status == "reserved"

    assert "library ledger: would refuse" not in logs.text
    assert sum(h.released_at is None for h in await _holds()) == 1


async def test_switching_to_a_cube_the_library_lacks_ends_the_hold(lounge, mode):
    await _borrower(ANN)
    await _queue("q1", [ANN])
    await resv.reserve("q1")
    await _set("q1", cube="some-other-cube")

    assert (await resv.cube_changed("q1")).status == "not_applicable"

    holds = await _holds("q1")
    assert len(holds) == 1 and holds[0].released_at is not None
    assert holds[0].released_by is None, "released by the hook, not healed"
