"""The room is told ONCE that the bot has given up, not once per manager.

On 2026-10-05 The Divination's draft channel received 68 copies of "Bot No
Longer Managing This Draft" between 19:29:25 and 20:49:05, one every 71
seconds, and on 2026-10-09 the Lounge's got 16 more in eighteen minutes. A
draft the bot could no longer claim had a fresh DraftSetupManager built for it
every minute, and each new instance called _stand_down -- whose docstring says
"Tell them once" -- for the first time.

What repeats is the MANAGER, so that is what these tests build: two instances
for one session, which is the unit the bug lives in. log_reconciler was where
the repetition came from, and it no longer rebuilds a manager the bot has been
refused (STOOD_DOWN, test_reconciler_stops_after_standdown_e2e) -- but the five
other callers of _notify_bot_no_longer_managing sit inside the ten-second
connection loop and can still repeat within one manager's life, and a restart
builds a fresh one. The guard has to hold without the reconciler's help, so
these tests no longer take it.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.draft_setup_manager as dsm
from conftest import make_manager

pytestmark = [pytest.mark.asyncio,
              pytest.mark.usefixtures("clean_manager_registry")]

SESSION, CHANNEL, DRAFT = "divination-1791217114", "1125467185335250964", "DBUDSVBH6O"
NOTICE = "Bot No Longer Managing This Draft"


@pytest.fixture(autouse=True)
def forget_what_the_room_was_told():
    """_TOLD_NOT_MANAGING lasts the life of the process, so it leaks between
    tests. Read with getattr so this file still runs against a tree without the
    guard: the regression below then fails on its own assertion, which says what
    went wrong, rather than on a missing name, which does not."""
    told = getattr(dsm, "_TOLD_NOT_MANAGING", set())
    told.clear()
    yield
    told.clear()


@pytest.fixture
def room(monkeypatch):
    """A channel that remembers every embed it was shown."""
    shown = []

    async def send(content=None, embed=None, view=None, **kwargs):
        shown.append(embed.title if embed is not None else content)
        return SimpleNamespace(id=999, edit=AsyncMock())

    channel = SimpleNamespace(name="cube-draft-open-play", send=send,
                              fetch_message=AsyncMock(side_effect=LookupError))
    bot = SimpleNamespace(get_channel=lambda _id: channel,
                          fetch_channel=AsyncMock(return_value=channel))
    monkeypatch.setattr(dsm, "get_bot", lambda: bot)
    return shown


async def _a_manager_gives_up(draft_id=DRAFT):
    """One manager for this session discovering it has lost the Draftmancer
    session, the way an ownership refusal on connect does.

    Built rather than fetched: a new instance per attempt IS the bug, so the
    test has to be able to make a second one without asking anything else to
    produce it for it.
    """
    manager = make_manager(session_id=SESSION, draft_id=draft_id, guild_id="g1")
    manager.draft_channel_id = CHANNEL
    await manager._stand_down("could not claim the Draftmancer session on connect")
    return manager


async def test_a_second_manager_for_one_session_says_nothing_new(test_db, room):
    """The regression. Three instances is 68 in production terms."""
    for _ in range(3):
        await _a_manager_gives_up()

    assert room.count(NOTICE) == 1, (
        f"the room was told {room.count(NOTICE)} times; every rebuilt manager's "
        f"'tell them once' was a first time")


async def test_a_regenerated_draftmancer_session_is_a_new_fact(test_db, room):
    """regenerate_draft_session mints a new session and announces it, so losing
    THAT one is something the room has not been told. Keyed per Draftmancer
    session for this reason, and not per draft."""
    await _a_manager_gives_up(draft_id=DRAFT)
    await _a_manager_gives_up(draft_id="DBREGEN02")

    assert room.count(NOTICE) == 2


async def test_a_channel_that_could_not_be_posted_to_is_told_later(test_db, room,
                                                                  monkeypatch):
    """Marked only on a send that worked. A Discord blip during the first
    stand-down must not buy silence for the rest of the process."""
    monkeypatch.setattr(dsm.DraftSetupManager, "_update_or_send_message",
                        AsyncMock(return_value=None))
    await _a_manager_gives_up()
    assert room.count(NOTICE) == 0, "the send failed, so nothing was shown"

    monkeypatch.undo()
    monkeypatch.setattr(dsm, "get_bot", lambda: SimpleNamespace(
        get_channel=lambda _id: SimpleNamespace(
            name="c", send=_recording(room),
            fetch_message=AsyncMock(side_effect=LookupError)),
        fetch_channel=AsyncMock()))
    await _a_manager_gives_up()

    assert room.count(NOTICE) == 1


def _recording(shown):
    async def send(content=None, embed=None, view=None, **kwargs):
        shown.append(embed.title if embed is not None else content)
        return SimpleNamespace(id=999, edit=AsyncMock())
    return send
