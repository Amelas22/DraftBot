"""No manager is rebuilt for a Draftmancer session the bot has been refused.

On 2026-10-05 a PowerLSV draft in The Divination fired after seven hours in the
queue, and the endDraft push carried no log -- so logs_captured_at stayed NULL
and log_reconciler kept selecting it. Every 60 seconds it built a fresh
DraftSetupManager; every one connected, got `setSessionOwner -> "Unautorized"`,
and stood down. 70 managers, 113 ticks, 19:14:08 to 21:28:06, and not one could
ever have succeeded: Draftmancer re-delivers the log only to a session the bot
can join as owner, and a human owned that one.

So standing down records the draft, and spawn_for_existing_session -- which
already owns the question "can a manager be built for this session", and
already answers no by returning None -- declines from then on.

The reconciler's own tests mock spawn_for_existing_session out, so they cannot
see this guard; the first test here drives the real one.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.draft_setup_manager as dsm
import services.log_reconciler as reconciler
from conftest import seed_session

pytestmark = [pytest.mark.asyncio,
              pytest.mark.usefixtures("clean_manager_registry")]

SESSION, CHANNEL = "divination-1791217114", "1125467185335250964"


@pytest.fixture(autouse=True)
def forget_what_was_given_up_on():
    """STOOD_DOWN lasts the life of the process, so it leaks between tests.

    Read with getattr so this file still runs against a tree without the fix:
    the regression below then fails on its own assertion, which says what went
    wrong, rather than on a missing name, which does not.
    """
    given_up = getattr(dsm, "STOOD_DOWN", set())
    given_up.clear()
    yield
    given_up.clear()


async def _uncaptured_draft():
    """A draft that fired and whose log never arrived -- the reconciler's target.

    `start` is what sets teams_start_time, the only time the capture query
    filters on, and seed_session leaves logs_captured_at NULL by never writing
    it. draft_channel_id is where a stand-down would be announced.
    """
    await seed_session(session_id=SESSION, stage="pairings", stype="random",
                       start=datetime.now() - timedelta(minutes=15),
                       draft_channel_id=CHANNEL, draft_id="DBUDSVBH6O",
                       sign_ups={"1": "NateM8"})


@pytest.fixture
def room(monkeypatch):
    """A bot that swallows what it is told, and no websocket."""
    channel = SimpleNamespace(name="cube-draft-open-play", send=AsyncMock(),
                              fetch_message=AsyncMock(side_effect=LookupError))
    bot = SimpleNamespace(get_channel=lambda _id: channel,
                          fetch_channel=AsyncMock(return_value=channel))
    monkeypatch.setattr(dsm, "get_bot", lambda: bot)
    monkeypatch.setattr(dsm.DraftSetupManager, "keep_connection_alive", AsyncMock())
    monkeypatch.setattr("services.log_reconciler.asyncio.sleep", AsyncMock())
    return bot


async def test_the_reconciler_gives_up_once_the_bot_has(test_db, room):
    """The regression: three ticks, one manager.

    The first tick builds a manager and Draftmancer refuses it. The two after it
    must build nothing -- in production they built one a minute for 2h14m.
    """
    await _uncaptured_draft()
    built = []
    real = dsm.DraftSetupManager.spawn_for_existing_session

    async def counting(session_id, bot):
        manager = await real(session_id, bot)
        built.append(manager)
        return manager
    dsm.DraftSetupManager.spawn_for_existing_session = counting
    try:
        await reconciler.reconcile_capture(room)
        manager = dsm.ACTIVE_MANAGERS[SESSION]
        await manager.fetch_draft_info()
        await manager._stand_down("could not claim the Draftmancer session")

        await reconciler.reconcile_capture(room)
        await reconciler.reconcile_capture(room)
    finally:
        dsm.DraftSetupManager.spawn_for_existing_session = real

    assert [m for m in built if m is not None] == [manager], (
        f"{len([m for m in built if m])} managers were built for a session the "
        f"bot had already been refused")


async def test_a_draft_the_bot_has_not_given_up_on_is_still_built(test_db, room):
    """The guard, and it needs its own test: the reconciler's existing tests
    patch spawn_for_existing_session out, so they cannot see this check at all.

    The retry exists for a real case -- a missed endDraft push on a session the
    bot still owns -- and refusing that one would lose logs rather than embeds.
    """
    await _uncaptured_draft()

    manager = await dsm.DraftSetupManager.spawn_for_existing_session(SESSION, room)

    assert manager is not None


async def test_giving_up_is_recorded_even_if_the_room_cannot_be_told(
        test_db, room):
    """Recorded before the notice, so a Discord failure cannot leave the
    reconciler coming back forever."""
    await _uncaptured_draft()
    manager = await dsm.DraftSetupManager.spawn_for_existing_session(SESSION, room)
    await manager.fetch_draft_info()
    manager._notify_bot_no_longer_managing = AsyncMock(
        side_effect=RuntimeError("Discord is down"))

    with pytest.raises(RuntimeError):
        await manager._stand_down("could not claim the Draftmancer session")

    assert SESSION in dsm.STOOD_DOWN
    assert await dsm.DraftSetupManager.spawn_for_existing_session(SESSION, room) is None
