"""The quiz paths must not load DraftSession.draft_data.

Both quiz selectors used to issue a bare `select(DraftSession)` across a year of
drafts. `draft_data` is a plain JSON column with no deferral, so every candidate
row decoded its full Draftmancer log and the whole list stayed alive for the
duration of selection -- 487 MB of decoded Python on a 1.96 GB box with no swap,
every time a quiz posted. That is what OOM-killed the bot daily.

Neither path ever reads the column: both fetch their log over the network via
`load_from_spaces(spaces_object_key)`. The view-reload path has the same shape
with a worse lifetime, restoring a full row into a persistent view.

Each test asserts at both layers -- the ORM's loaded state, and the SQL actually
emitted -- because they fail independently: a row can look unloaded because it
was expired rather than deferred, and SQL can omit the column while some later
option re-adds it.
"""
import random
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, inspect

import cogs.quiz_commands as quiz_commands
import cogs.trophy_quiz_commands as trophy_quiz_commands
from conftest import seed_session
from database.db_session import AsyncSessionLocal
from models import QuizSession
from quiz_views_module.quiz_views import QuizPublicView


def pick_quiz_log():
    """6-player draft, pack 0 passing left; enough picks for a 4-pick trace."""
    users = {}
    for i in range(6):
        users[f"user{i}"] = {
            "userName": f"Player{i}",
            "seatNum": i,
            "picks": [{"packNum": 0, "pickNum": i, "pick": [0],
                       "booster": [f"c{j}" for j in range(i, 6)]}],
        }
    return {
        "sessionID": "TEST_SESSION",
        "users": users,
        "carddata": {f"c{i}": {"name": f"Card{i}"} for i in range(6)},
    }


def trophy_log():
    users, carddata = {}, {}
    for i in range(6):
        users[f"dm{i}"] = {"seatNum": i, "cards": [f"c{i}"], "isBot": False}
        carddata[f"c{i}"] = {"name": f"Card{i}"}
    return {"users": users, "carddata": carddata}


# 6 drafters over 3 rounds, with a 3-0 and a 0-3 so select_two_decks finds an
# extreme and the trophy selector accepts the draft.
_WITH_EXTREME = [
    ("d0", "d1", "d0", None), ("d2", "d3", "d2", None), ("d4", "d5", "d4", None),
    ("d0", "d2", "d0", None), ("d1", "d4", "d1", None), ("d3", "d5", "d3", None),
    ("d0", "d3", "d0", None), ("d1", "d5", "d1", None), ("d2", "d4", "d2", None),
]


async def seed_eligible(session_id, log, matches=()):
    """A quiz-eligible draft whose fat column is POPULATED.

    Populating it is what lets the assertions distinguish a deferred query from
    an eager one: a NULL column would read as 'loaded' either way.

    The start time is relative on purpose. Both selectors filter on
    `draft_start_time >= now - 365 days`, so conftest's hardcoded default would
    quietly age out of the window and turn these tests red on a date rather than
    on a regression.
    """
    await seed_session(
        session_id=session_id, guild="g1", stype="random", stage="completed",
        sign_ups={f"d{i}": f"n{i}" for i in range(6)}, cube="TestCube",
        matches=matches, draft_data=log,
        spaces_object_key=f"key-{session_id}",
        start=datetime.now() - timedelta(days=1),
    )


@contextmanager
def captured_sql(engine):
    """Collect every statement the engine emits inside the block."""
    statements = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)


def assert_draft_data_absent(statements):
    selects = [s for s in statements if "FROM draft_sessions" in s]
    assert selects, "no query was issued against draft_sessions"
    assert not any("draft_data" in s for s in selects), (
        "draft_data still appears in the emitted SQL:\n" + "\n".join(selects))


@pytest.mark.asyncio
async def test_pick_selector_does_not_load_draft_data(test_db):
    log = pick_quiz_log()
    await seed_eligible("d-pick", log)

    cog = quiz_commands.QuizCommands(bot=None)
    with captured_sql(test_db) as statements, \
         patch("services.draft_analysis.load_from_spaces",
               AsyncMock(return_value=log)):
        draft, seat = await cog._select_random_draft_and_seat("g1")

    assert draft is not None and seat is not None
    # Assert before touching any other attribute: reading a deferred attribute
    # would load it and destroy the thing under test.
    assert "draft_data" in inspect(draft).unloaded
    assert_draft_data_absent(statements)


@pytest.mark.asyncio
async def test_trophy_selector_does_not_load_draft_data(test_db):
    log = trophy_log()
    await seed_eligible("d-trophy", log, matches=_WITH_EXTREME)

    # Return a payload the column does NOT contain, so the assertion below
    # identifies where the log actually came from.
    from_spaces = {**log, "sessionID": "FROM_SPACES"}
    with captured_sql(test_db) as statements, \
         patch("cogs.trophy_quiz_commands.load_from_spaces",
               AsyncMock(return_value=from_spaces)):
        draft, decks, draft_data = await trophy_quiz_commands._select_eligible_draft(
            "g1", rng=random.Random(0))

    assert draft is not None
    assert "draft_data" in inspect(draft).unloaded
    assert_draft_data_absent(statements)
    assert decks is not None and len(decks) == 2
    assert draft_data["sessionID"] == "FROM_SPACES"


@pytest.mark.asyncio
async def test_view_reload_does_not_load_draft_data(test_db):
    """The persistent view keeps this row for the life of the process."""
    log = pick_quiz_log()
    await seed_eligible("d-view", log)
    async with AsyncSessionLocal() as s:
        async with s.begin():
            s.add(QuizSession(
                quiz_id="q1", display_id=1, guild_id="g1", channel_id="c1",
                draft_session_id="d-view", starting_seat=0,
                pack_trace_data={"picks": []}, correct_answers=[],
                posted_by="mod",
            ))

    view = QuizPublicView("q1")
    with captured_sql(test_db) as statements, \
         patch("services.draft_analysis.load_from_spaces",
               AsyncMock(return_value=log)):
        loaded = await view._load_quiz_data()

    assert loaded
    assert_draft_data_absent(statements)


@pytest.mark.asyncio
async def test_quiz_embed_builds_from_deferred_row(test_db):
    """A trimmed row must still work where the quiz actually reads it.

    create_quiz_embed reads cube and draft_start_time off the selected row.
    tests/test_quiz_pack_image.py deliberately aborts before the embed, so
    nothing else covers this boundary.
    """
    log = pick_quiz_log()
    await seed_eligible("d-embed", log)

    cog = quiz_commands.QuizCommands(bot=None)
    # create_pack_visualization_url POSTs the draft to magicprotools.com, and
    # MPT_API_KEY is in .env, so an unpatched run would upload real draft data to
    # a third party. It currently only escapes that because this fixture has no
    # "time" key and the resulting KeyError is swallowed -- which would stop
    # protecting us the moment anyone completes the fixture.
    with patch("services.draft_analysis.load_from_spaces",
               AsyncMock(return_value=log)), \
         patch("cogs.quiz_commands.load_from_spaces",
               AsyncMock(return_value=log)), \
         patch.object(quiz_commands.QuizCommands, "create_pack_visualization_url",
                      AsyncMock(return_value=None)):
        draft, seat = await cog._select_random_draft_and_seat("g1")
        prepared = await cog._prepare_quiz_data(draft, seat)

    # Unpacking a None return would surface as a TypeError rather than a
    # readable failure.
    assert prepared is not None, "could not prepare a quiz from a deferred row"
    analysis, pack_trace, _, _ = prepared

    embed = cog.create_quiz_embed(draft, pack_trace, analysis, display_id=7)

    assert "TestCube" in embed.fields[0].value
    # Nothing in the prepare/embed path faulted the column back in.
    assert "draft_data" in inspect(draft).unloaded
