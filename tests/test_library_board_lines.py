"""A board's library line stays true: the library publishes, the board redraws."""
from datetime import datetime, timedelta

import discord
import pytest
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession
from services import library_boards as boards
from services import library_events as bus
import cube_views.pack_options as po


async def _board(sid="d1", guild="g1", **kw):
    async with AsyncSessionLocal() as s:
        kw.setdefault("message_id", "m-" + sid)
        s.add(DraftSession(session_id=sid, guild_id=guild, cube="mycube", sign_ups={},
                           deletion_time=datetime.now() + timedelta(hours=3), **kw))
        await s.commit()


@pytest.fixture
def world(monkeypatch):
    """The board's library answers, and every redraw, recorded."""
    state = {"line": "🏛️ covered", "redraws": [], "library": "lib", "asked_for": []}

    async def check(_cube, _guild, session_id=None):
        state["asked_for"].append(session_id)
        if isinstance(state["line"], Exception):
            raise state["line"]
        return state["line"]

    state["lookups"] = []

    async def library_id_for(guild):
        state["lookups"].append(guild)
        return state["library"] if guild == "g1" else None

    async def redraw(_bot, sid):
        state["redraws"].append(sid)

    monkeypatch.setattr(po, "library_signup_check", check)
    monkeypatch.setattr("services.library_service.library_id_for", library_id_for)
    monkeypatch.setattr(boards, "_redraw", redraw)
    return state


@pytest.mark.asyncio
async def test_a_changed_line_is_stored_and_redrawn(test_db, world):
    await _board()
    await boards.watch("d1", "g1", "🏛️ covered")
    world["line"] = "⏳ in use"
    bus.publish("lib")
    await bus.drain()
    assert boards.line_for("d1") == "⏳ in use" and world["redraws"] == ["d1"]


@pytest.mark.asyncio
async def test_a_board_asks_for_its_own_draft(test_db, world):
    """So its own queue's reservation is never "another queue"."""
    await _board()
    await boards.watch("d1", "g1", "🏛️ covered")
    bus.publish("lib")
    await bus.drain()
    assert world["asked_for"] == ["d1"]


@pytest.mark.asyncio
async def test_an_unchanged_line_makes_no_edit(test_db, world):
    await _board()
    await boards.watch("d1", "g1", "🏛️ covered")
    bus.publish("lib")
    await bus.drain()
    assert world["redraws"] == []


@pytest.mark.asyncio
async def test_a_failed_check_keeps_the_old_line(test_db, world):
    await _board()
    await boards.watch("d1", "g1", "🏛️ covered")
    world["line"] = po.LibraryLineUnknown("cube unreadable")
    bus.publish("lib")
    await bus.drain()
    assert boards.line_for("d1") == "🏛️ covered" and world["redraws"] == []


@pytest.mark.asyncio
async def test_a_board_whose_teams_formed_stops_listening(test_db, world):
    await _board(teams_start_time=datetime.now())
    await boards.watch("d1", "g1", "🏛️ covered")
    world["line"] = "⏳ in use"
    bus.publish("lib")
    await bus.drain()
    assert not bus._heard("lib")
    bus.publish("lib")
    await bus.drain()
    assert world["redraws"] == [] and boards.line_for("d1") == "🏛️ covered"


@pytest.mark.asyncio
async def test_a_board_in_a_server_without_a_library_is_untracked(test_db, world):
    await _board(guild="g-none")
    await boards.watch("d1", "g-none", None)
    assert boards.line_for("d1") is boards.UNTRACKED


def _embed(*names):
    e = discord.Embed(title="t")
    for n in names:
        e.add_field(name=n, value="v", inline=n != po.LIBRARY_FIELD_NAME)
    return e


def test_the_field_is_added_after_the_cube_replaced_and_removed():
    e = _embed("Sign-Ups:", "Cube:", "Pack format:")
    po.apply_library_field(e, "🏛️ covered")
    assert [f.name for f in e.fields] == ["Sign-Ups:", "Cube:", po.LIBRARY_FIELD_NAME, "Pack format:"]
    po.apply_library_field(e, "⏳ in use")
    assert e.fields[2].value == "⏳ in use" and len(e.fields) == 4
    po.apply_library_field(e, None)
    assert po.LIBRARY_FIELD_NAME not in [f.name for f in e.fields]


@pytest.mark.asyncio
async def test_the_other_queues_board_switches_back_when_a_reservation_ends(test_db, world, monkeypatch):
    """End to end through the hooks: a write to draft d2 publishes for g1, and d1 redraws."""
    from services import library_publish
    library_publish.install()
    await _board("d1")
    await _board("d2", library_reserved_at=datetime.now())
    await boards.watch("d1", "g1", "⏳ in use")
    world["line"] = "🏛️ covered"
    async with AsyncSessionLocal() as s:
        d2 = (await s.execute(select(DraftSession).where(DraftSession.session_id == "d2"))).scalar_one()
        d2.session_stage = "completed"
        await s.commit()
    await bus.drain()
    assert world["redraws"] == ["d1"] and boards.line_for("d1") == "🏛️ covered"


@pytest.mark.asyncio
async def test_a_pending_board_whose_first_line_is_empty_is_redrawn(test_db, world):
    """Loaded at startup with a stale field from before the restart: an empty
    first line must still redraw, so the stale field comes off."""
    await _board()
    await boards.watch("d1", "g1", None)
    boards._LINES.pop("d1")             # what load_boards leaves: watched, not computed
    world["line"] = None
    bus.publish("lib")
    await bus.drain()
    assert boards.line_for("d1") is None and world["redraws"] == ["d1"]


@pytest.mark.asyncio
async def test_a_second_load_keeps_a_line_already_known(test_db, world):
    """on_ready runs again after a gateway reconnect; a line already true stays."""
    from models.library import Library
    from models.library_server import LibraryServer
    async with AsyncSessionLocal() as s:
        s.add(Library(id="lib", name="Lib"))
        s.add(LibraryServer(library_id="lib", guild_id="g1"))
        await s.commit()
    await _board()
    await boards.load_boards()
    await bus.drain()
    assert boards.line_for("d1") == "🏛️ covered" and world["redraws"] == ["d1"]
    await boards.load_boards()
    assert boards.line_for("d1") == "🏛️ covered"
    await bus.drain()
    assert world["redraws"] == ["d1"]


@pytest.mark.asyncio
async def test_a_board_not_yet_posted_stores_its_line_without_a_redraw(test_db, world):
    await _board(message_id=None)
    await boards.watch("d1", "g1", "🏛️ covered")
    world["line"] = "⏳ in use"
    bus.publish("lib")
    await bus.drain()
    assert boards.line_for("d1") == "⏳ in use" and world["redraws"] == []


@pytest.mark.asyncio
async def test_loading_resolves_each_servers_library_once(test_db, world):
    from models.library import Library
    from models.library_server import LibraryServer
    async with AsyncSessionLocal() as s:
        s.add(Library(id="lib", name="Lib"))
        s.add(LibraryServer(library_id="lib", guild_id="g1"))
        await s.commit()
    for sid in ("d1", "d2", "d3"):
        await _board(sid)
    world["lookups"].clear()
    await boards.load_boards()
    await bus.drain()
    assert world["lookups"] == ["g1"]
    assert sorted(world["redraws"]) == ["d1", "d2", "d3"]
