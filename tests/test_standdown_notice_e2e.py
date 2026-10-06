"""The room is told ONCE that the bot has given up, not once a minute.

On 2026-10-05 The Divination's draft channel received 68 copies of "Bot No
Longer Managing This Draft" between 19:29:25 and 20:49:05, one every 71
seconds. A PowerLSV draft had fired after seven hours in the queue; the bot
could no longer claim the Draftmancer session (`setSessionOwner ->
"Unautorized"`) and the endDraft push carried no log, so logs_captured_at
stayed NULL. log_reconciler then rebuilt a DraftSetupManager every 60 seconds,
and each new instance called _stand_down -- whose docstring says "Tell them
once" -- for the first time.

Driven through the real reconciler, the real manager construction and the real
notifier; only the websocket and Discord are stood in for. The churn the bug
needs is the reconciler's own: standing down deregisters the manager, so the
next tick builds another, which is what production did 70 times.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

import services.draft_setup_manager as dsm
import services.log_reconciler as reconciler
from conftest import seed_session
from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession

pytestmark = [pytest.mark.asyncio,
              pytest.mark.usefixtures("clean_manager_registry")]

SESSION, CHANNEL = "divination-1791217114", "1125467185335250964"
NOTICE = "Bot No Longer Managing This Draft"


@pytest.fixture(autouse=True)
def forget_what_the_room_was_told():
    """_TOLD_NOT_MANAGING lasts the life of the process, so it leaks between
    tests. Read with getattr so this file still runs against a tree without the
    fix: the regression below then fails on its own assertion, which says what
    went wrong, rather than on a missing name, which does not."""
    told = getattr(dsm, "_TOLD_NOT_MANAGING", set())
    told.clear()
    yield
    told.clear()


async def _uncaptured_draft():
    """A draft that fired and whose log never arrived -- the reconciler's target.

    `start` is what sets teams_start_time, the only time the capture query
    filters on, and seed_session leaves logs_captured_at NULL by never writing
    it. draft_channel_id is where the notice lands.
    """
    await seed_session(session_id=SESSION, stage="pairings", stype="random",
                       start=datetime.now() - timedelta(minutes=15),
                       draft_channel_id=CHANNEL, draft_id="DBUDSVBH6O",
                       sign_ups={"1": "NateM8"})


@pytest.fixture
def room(monkeypatch):
    """The bot, a channel that remembers every embed, and no websocket.

    keep_connection_alive is all that is stood in for on the manager: it is what
    would dial Draftmancer, and its refusal is what _tick delivers by hand.
    """
    shown = []

    async def send(content=None, embed=None, view=None):
        shown.append(embed.title if embed is not None else content)
        return SimpleNamespace(id=999, edit=AsyncMock())

    channel = SimpleNamespace(name="cube-draft-open-play", send=send,
                              fetch_message=AsyncMock(side_effect=LookupError))
    bot = SimpleNamespace(get_channel=lambda _id: channel,
                          fetch_channel=AsyncMock(return_value=channel))
    monkeypatch.setattr(dsm, "get_bot", lambda: bot)
    monkeypatch.setattr(dsm.DraftSetupManager, "keep_connection_alive", AsyncMock())
    monkeypatch.setattr("services.log_reconciler.asyncio.sleep", AsyncMock())
    return bot, shown


async def _tick(bot):
    """One reconciler pass, ending the way production's did.

    The reconciler spawns a manager and waits for a log that never arrives.
    Draftmancer then refuses the ownership claim, which is what reaches
    _stand_down -- delivered here directly because the refusal arrives over the
    websocket this test does not open.

    fetch_draft_info is awaited rather than left to the background task
    set_bot_instance creates for it: it is a pure database read that gives the
    manager its draft_channel_id, and in production it had always finished long
    before an ownership refusal came back over the wire. Without it the notice
    has nowhere to go and the test passes for the wrong reason.
    """
    await reconciler.reconcile_capture(bot)
    manager = dsm.ACTIVE_MANAGERS.get(SESSION)
    assert manager is not None, "the reconciler should have built a manager"
    await manager.fetch_draft_info()
    assert manager.draft_channel_id == CHANNEL, "the notice needs a channel"
    await manager._stand_down("could not claim the Draftmancer session on connect")
    assert SESSION not in dsm.ACTIVE_MANAGERS, (
        "standing down must deregister, or the next tick reuses this manager "
        "and the bug cannot reproduce")


async def test_three_reconciler_ticks_tell_the_room_once(test_db, room):
    """The regression. Three ticks is 68 in production terms."""
    bot, shown = room
    await _uncaptured_draft()

    for _ in range(3):
        await _tick(bot)

    assert shown.count(NOTICE) == 1, (
        f"the room was told {shown.count(NOTICE)} times; the reconciler "
        f"rebuilds the manager every tick and each instance said it once")


async def test_a_regenerated_draftmancer_session_is_a_new_fact(test_db, room):
    """regenerate_draft_session mints a new session and announces it, so losing
    THAT one is something the room has not been told. Keyed per Draftmancer
    session for this reason."""
    bot, shown = room
    await _uncaptured_draft()

    await _tick(bot)
    async with AsyncSessionLocal() as session:
        await session.execute(update(DraftSession)
                              .where(DraftSession.session_id == SESSION)
                              .values(draft_id="DBREGEN02"))
        await session.commit()
    await _tick(bot)

    assert shown.count(NOTICE) == 2
