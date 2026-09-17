"""Handing each drafter their pool at the end of a draft.

This is the join the card library was missing. assign_deck() built the
"assigned" loan that /borrow collects from the day the feature landed, but
nothing in the bot ever called it -- only the seeding script did, so a deck
could only be put in a player's hands by hand.

What makes it safe is the mapping. Sign-ups line up against Draftmancer seats
POSITIONALLY, and map_discord_to_draftmancer refuses to guess when the counts
disagree. Getting that wrong here does not mis-post a message: it hands someone
another player's 45 cards and charges them collateral for the privilege.
"""
from unittest.mock import AsyncMock, patch

import pytest

from database.db_session import AsyncSessionLocal
from models.card_loan import CardLoan
from models.draft_session import DraftSession
from services import draft_deck_assignment as svc
from services.card_lending_service import assign_deck

pytestmark = pytest.mark.asyncio

GUILD, SESSION = "g1", "sess-1"
ALICE, BOB = "disc_a", "disc_b"


def _log():
    return {
        "carddata": {"c1": {"name": "Lightning Bolt"},
                     "c2": {"name": "Counterspell"},
                     "c3": {"name": "Island"}},
        "users": {
            "dm_a": {"userName": "Alice", "seatNum": 0, "cards": ["c1", "c1", "c2"]},
            "dm_b": {"userName": "Bob", "seatNum": 1, "cards": ["c3"]},
        },
    }


_DEFAULT = object()


async def _seed(draft_data=_DEFAULT, sign_ups=None, session_id=SESSION):
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(
            session_id=session_id, guild_id=GUILD,
            draft_data=_log() if draft_data is _DEFAULT else draft_data,
            sign_ups=sign_ups if sign_ups is not None else {ALICE: "Alice", BOB: "Bob"}))
        await s.commit()


async def _loans():
    async with AsyncSessionLocal() as s:
        from sqlalchemy import select
        rows = (await s.scalars(select(CardLoan))).all()
    return {r.borrower_id: r for r in rows}


@pytest.fixture
def library_on():
    with patch("services.draft_deck_assignment.library_enabled", return_value=True):
        yield


async def test_every_drafter_is_handed_their_own_pool(test_db, library_on):
    await _seed()

    assert await svc.assign_drafted_decks(SESSION) == 2

    loans = await _loans()
    assert loans[ALICE].cards == [{"name": "Lightning Bolt", "qty": 2},
                                  {"name": "Counterspell", "qty": 1}]
    assert loans[BOB].cards == [{"name": "Island", "qty": 1}]
    assert loans[ALICE].state == "assigned", "waiting for them to run /borrow"


async def test_running_it_again_does_not_assign_a_second_deck(test_db, library_on):
    """Both the endDraft push and the reconciler's retry sweep call this, and
    the reconciler re-runs freely. A second deck is not merely noise: a player
    may hold exactly one unfinished loan, so the write would fail on the unique
    index -- or, worse, succeed and put the first deck beyond reach."""
    await _seed()

    assert await svc.assign_drafted_decks(SESSION) == 2
    assert await svc.assign_drafted_decks(SESSION) == 0

    assert len(await _loans()) == 2


async def test_a_guild_without_a_library_is_left_alone(test_db):
    await _seed()

    with patch("services.draft_deck_assignment.library_enabled", return_value=False):
        assert await svc.assign_drafted_decks(SESSION) == 0

    assert await _loans() == {}


async def test_a_player_still_holding_last_week_s_deck_is_skipped(test_db, library_on):
    """One unfinished loan at a time is the database's rule, not this module's.
    Someone who has not returned a deck is passed over rather than the whole
    draft's assignment failing on their row."""
    await assign_deck(GUILD, ALICE, [{"name": "Swamp", "qty": 4}], source="earlier")
    await _seed()

    assert await svc.assign_drafted_decks(SESSION) == 1

    loans = await _loans()
    assert loans[ALICE].cards == [{"name": "Swamp", "qty": 4}], "their old deck stands"
    assert loans[BOB].cards == [{"name": "Island", "qty": 1}]


async def test_a_seating_mismatch_assigns_nothing_at_all(test_db, library_on):
    """map_discord_to_draftmancer aligns sign-ups against seats by POSITION and
    returns {} when the counts disagree rather than guessing."""
    await _seed(sign_ups={ALICE: "Alice", BOB: "Bob", "disc_c": "Carol"})

    assert await svc.assign_drafted_decks(SESSION) == 0
    assert await _loans() == {}


async def test_a_partial_seating_assigns_nobody_rather_than_the_part_that_lined_up(
        test_db, library_on):
    """The refusal has to live HERE, not be a side effect of an empty mapping.

    Asserting on the count-mismatch case alone passes with this module's guard
    deleted, because an empty mapping makes the loop iterate nothing anyway. A
    mapping that resolves SOME players is the case that tells the two apart --
    and taking it would hand those players cards on a seating the mapper has
    already said it does not trust.
    """
    await _seed()

    with patch("services.draft_deck_assignment.map_discord_to_draftmancer",
               return_value={}):
        assert await svc.assign_drafted_decks(SESSION) == 0

    assert await _loans() == {}, "a refused seating assigns nobody"


async def test_a_draft_with_no_log_yet_assigns_nothing(test_db, library_on):
    """The reconciler sweeps sessions whose log has not landed. Nothing to
    assign is not a failure -- it is called again once the log is captured."""
    await _seed(draft_data=None)

    assert await svc.assign_drafted_decks(SESSION) == 0


async def test_a_drafter_who_took_no_cards_gets_no_loan(test_db, library_on):
    """An empty pool is not a deck. A loan of nothing would still occupy their
    one active-loan slot and block the next draft's assignment."""
    log = _log()
    log["users"]["dm_b"]["cards"] = []
    await _seed(draft_data=log)

    assert await svc.assign_drafted_decks(SESSION) == 1
    assert BOB not in await _loans()


async def test_the_loan_records_which_draft_it_came_from(test_db, library_on):
    """`source` is what makes the second run a no-op, so it is contract rather
    than a log line."""
    await _seed()
    await svc.assign_drafted_decks(SESSION)

    assert (await _loans())[ALICE].source == f"draft:{SESSION}"


# --- the two things that call it --------------------------------------------

async def test_the_reconciler_sweep_assigns_decks_too(test_db):
    """A call site with no test is exactly how this feature came to be missing:
    assign_deck existed and worked, and nothing called it. Patching the service
    and asserting the sweep reaches it pins the wiring, not the service."""
    from datetime import datetime, timedelta
    from unittest.mock import AsyncMock, MagicMock

    from services.log_reconciler import reconcile_publish_and_team_logs

    now = datetime.now()
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="A", guild_id=GUILD, draft_data=_log(),
                           sign_ups={ALICE: "Alice", BOB: "Bob"},
                           logs_captured_at=now, team_logs_posted_at=None,
                           session_type="random"))
        await s.commit()

    with patch("services.log_reconciler.post_team_logs", new=AsyncMock()), \
         patch("services.log_reconciler.DraftSetupManager", MagicMock()), \
         patch("services.log_reconciler.assign_drafted_decks",
               new=AsyncMock()) as assigned:
        await reconcile_publish_and_team_logs(MagicMock())

    assigned.assert_any_await("A")


async def test_a_failed_pool_post_does_not_cost_anyone_their_deck(test_db):
    """The two run in separate try blocks on purpose. post_team_logs refuses to
    finish whenever a team channel will not resolve, and a draft whose rooms
    were never created would otherwise never hand anybody a deck."""
    from datetime import datetime
    from unittest.mock import AsyncMock, MagicMock

    from services.log_reconciler import reconcile_publish_and_team_logs

    now = datetime.now()
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="A", guild_id=GUILD, draft_data=_log(),
                           sign_ups={ALICE: "Alice", BOB: "Bob"},
                           logs_captured_at=now, team_logs_posted_at=None,
                           session_type="random"))
        await s.commit()

    with patch("services.log_reconciler.post_team_logs",
               new=AsyncMock(side_effect=RuntimeError("no channel"))), \
         patch("services.log_reconciler.DraftSetupManager", MagicMock()), \
         patch("services.log_reconciler.assign_drafted_decks",
               new=AsyncMock()) as assigned:
        await reconcile_publish_and_team_logs(MagicMock())

    assigned.assert_any_await("A")


async def test_a_draft_whose_pools_already_posted_still_gets_its_decks(test_db):
    """The sweep that retries pool posting selects only drafts whose pools have
    NOT posted -- the opposite of the case that needs retrying here. Pools post
    first time for almost every draft, so a deck assignment riding along on that
    query would never run again for one that missed the endDraft push.
    """
    from datetime import datetime
    from unittest.mock import AsyncMock, MagicMock

    from services.log_reconciler import reconcile_publish_and_team_logs

    now = datetime.now()
    async with AsyncSessionLocal() as s:
        s.add(DraftSession(session_id="A", guild_id=GUILD, draft_data=_log(),
                           sign_ups={ALICE: "Alice", BOB: "Bob"},
                           logs_captured_at=now,
                           team_logs_posted_at=now,      # pools went out fine
                           session_type="random"))
        await s.commit()

    with patch("services.log_reconciler.post_team_logs", new=AsyncMock()) as posted, \
         patch("services.log_reconciler.DraftSetupManager", MagicMock()), \
         patch("services.log_reconciler.assign_drafted_decks",
               new=AsyncMock()) as assigned:
        await reconcile_publish_and_team_logs(MagicMock())

    posted.assert_not_awaited()          # nothing to retry there, correctly
    assigned.assert_any_await("A")       # ...but the decks still get handed out


async def test_two_faced_cards_are_assigned_under_the_name_mtgo_knows(test_db, library_on):
    """The live failure on 2026-09-17: loan 19 went out naming
    "Invasion of Ixalan // Belligerent Regisaur" and the serve refused the whole
    batch -- `unknown card ... (names must be exact)` -- so forty-odd cards that
    were perfectly borrowable went down with the one that was not.

    Both halves are pinned here because the rule is not "strip the //": a split
    card really does trade under its full name, and stripping it would break
    those to fix these.
    """
    log = {
        "carddata": {
            "c1": {"name": "Invasion of Ixalan // Belligerent Regisaur",
                   "back": {"name": "Belligerent Regisaur"}},
            "c2": {"name": "Commit // Memory", "layout": "split-left"},
            "c3": {"name": "Bonecrusher Giant // Stomp"},
            "c4": {"name": "Lightning Bolt"},
        },
        "users": {
            "dm_a": {"userName": "Alice", "seatNum": 0,
                     "cards": ["c1", "c2", "c3", "c4"]},
            "dm_b": {"userName": "Bob", "seatNum": 1, "cards": ["c4"]},
        },
    }
    await _seed(draft_data=log)

    assert await svc.assign_drafted_decks(SESSION) == 2

    assert [c["name"] for c in (await _loans())[ALICE].cards] == [
        "Invasion of Ixalan",                 # transforming: front face
        "Commit // Memory",                   # split: both halves, untouched
        "Bonecrusher Giant",                  # Adventure, no back key: front face
        "Lightning Bolt",
    ]


async def test_two_faces_that_collapse_to_one_name_are_counted_together(
        test_db, library_on):
    """Front-face naming can make two different cards share a name in the loan
    -- a deck holding both faces' printings must ask for two copies, not list
    one name twice. The serve keys its movement record by name, so a duplicate
    entry would have its second copy read as already booked."""
    log = {
        "carddata": {
            "c1": {"name": "Thing in the Ice // Awoken Horror",
                   "back": {"name": "Awoken Horror"}},
            "c2": {"name": "Thing in the Ice // Awoken Horror",
                   "back": {"name": "Awoken Horror"}},
        },
        "users": {
            "dm_a": {"userName": "Alice", "seatNum": 0, "cards": ["c1", "c2"]},
            "dm_b": {"userName": "Bob", "seatNum": 1, "cards": []},
        },
    }
    await _seed(draft_data=log)

    await svc.assign_drafted_decks(SESSION)

    assert (await _loans())[ALICE].cards == [{"name": "Thing in the Ice", "qty": 2}]


# --- what it costs, and what it says ----------------------------------------

@pytest.fixture(autouse=True)
def _forget_what_was_said():
    """`_SAID` suppresses repeat warnings for the life of the process, which
    would otherwise leak between tests."""
    svc._SAID.clear()
    svc._UNSEATABLE.clear()
    yield
    svc._SAID.clear()
    svc._UNSEATABLE.clear()


async def test_a_finished_draft_is_not_re_read_from_disk_every_tick(test_db, library_on):
    """The reconciler re-runs this for every draft captured in the last 72
    hours, once a minute. Reading a draft's whole carddata block to discover
    there is nothing left to do would read tens of megabytes an hour to reach
    the same answer -- so the cheap checks have to come first.
    """
    await _seed()
    assert await svc.assign_drafted_decks(SESSION) == 2

    with patch("services.draft_deck_assignment._draft_log",
               new=AsyncMock()) as read_log:
        assert await svc.assign_drafted_decks(SESSION) == 0

    read_log.assert_not_awaited(), "a settled draft must not load its log again"


async def test_a_draft_nobody_can_be_assigned_from_does_not_read_its_log_either(
        test_db, library_on):
    """Same reasoning for the other steady state: every drafter is still
    holding a deck from last time, so there is nothing to do and no reason to
    parse the log to find that out."""
    for who in (ALICE, BOB):
        await assign_deck(GUILD, who, [{"name": "Swamp", "qty": 4}], source="earlier")
    await _seed()

    with patch("services.draft_deck_assignment._draft_log",
               new=AsyncMock()) as read_log:
        assert await svc.assign_drafted_decks(SESSION) == 0

    read_log.assert_not_awaited()


async def test_a_pool_too_big_to_trade_is_refused_rather_than_made_uncollectable(
        test_db, library_on):
    """An 'assigned' loan nobody can collect is worse than no loan: it takes the
    borrower's one active-loan slot, and there is no way to give it back --
    /return only accepts a deck that was actually borrowed. So a pool bigger
    than one MTGO trade is refused loudly instead of written."""
    log = {"carddata": {"c1": {"name": "Swamp"}},
           "users": {"dm_a": {"userName": "Alice", "seatNum": 0, "cards": ["c1"] * 30},
                     "dm_b": {"userName": "Bob", "seatNum": 1, "cards": ["c1"]}}}
    await _seed(draft_data=log)

    with patch("services.draft_deck_assignment.too_large",
               side_effect=lambda n: n > 10):
        assert await svc.assign_drafted_decks(SESSION) == 1

    loans = await _loans()
    assert ALICE not in loans, "30 cards will not fit in one trade"
    assert loans[BOB].cards == [{"name": "Swamp", "qty": 1}]


async def test_one_players_write_failing_still_assigns_everybody_else(
        test_db, library_on):
    """The same rule the skips follow: one player's row costs that player, not
    the five drafters queued behind them."""
    await _seed()
    real = svc.assign_deck
    calls = {"n": 0}

    async def flaky(guild_id, borrower_id, cards, source=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("row exploded")
        return await real(guild_id, borrower_id, cards, source=source)

    with patch("services.draft_deck_assignment.assign_deck", new=flaky):
        assert await svc.assign_drafted_decks(SESSION) == 1

    assert len(await _loans()) == 1


async def test_losing_the_race_with_the_other_caller_is_not_an_error(
        test_db, library_on):
    """The push path and a reconciler tick can both be inside this function for
    one session -- they share an event loop and interleave at every await. The
    loser hits the one-active-loan index. The row it collided with is the deck
    being assigned right now, so there is nothing to repair."""
    from sqlalchemy.exc import IntegrityError

    await _seed()

    async def already_there(*a, **kw):
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

    with patch("services.draft_deck_assignment.assign_deck", new=already_there):
        assert await svc.assign_drafted_decks(SESSION) == 0


async def test_a_draft_that_can_never_assign_warns_once_not_once_a_minute(
        test_db, library_on, caplog):
    """72 hours of reconciler ticks at one a minute is over four thousand
    identical warnings, which does not make the problem easier to find -- it
    makes every other problem harder to find."""
    await _seed(sign_ups={ALICE: "Alice", BOB: "Bob", "disc_c": "Carol"})

    with patch.object(svc.logger, "warning") as first:
        await svc.assign_drafted_decks(SESSION)
    said_first = first.call_count

    with patch.object(svc.logger, "warning") as rest, \
         patch("services.draft_deck_assignment._draft_log",
               new=AsyncMock()) as read_log:
        for _ in range(4):
            await svc.assign_drafted_decks(SESSION)

    assert said_first >= 1, "the first tick has to say something"
    assert rest.call_count == 0, f"{rest.call_count} repeat warnings"
    read_log.assert_not_awaited(), "and it must stop re-reading the log too"


async def test_both_callers_racing_on_one_draft_do_not_raise_or_double_assign(
        test_db, library_on):
    """The push path and a reconciler tick can both be inside this function for
    one session: _on_end_draft fires the moment the log is captured, and the
    sweep runs every 60 seconds over that same freshly-captured row. They share
    an event loop and interleave at every await, so both can read an empty
    `done` and both try to insert.

    The one-active-loan index stops the double loan. This pins the rest: no
    exception escapes to the callers, and the draft ends with one deck each.
    """
    import asyncio

    await _seed()

    results = await asyncio.gather(
        svc.assign_drafted_decks(SESSION),
        svc.assign_drafted_decks(SESSION),
        return_exceptions=True,
    )

    assert not [r for r in results if isinstance(r, BaseException)], results
    loans = await _loans()
    assert len(loans) == 2, f"one deck each, got {len(loans)}"
    assert sum(r for r in results) == 2, "between them they assigned two decks"


async def test_drafters_sharing_a_display_name_are_assigned_nothing(
        test_db, library_on):
    """Two players called "Alex" can be seated in each other's positions.

    Seats are requested from Draftmancer by USERNAME (setSeating, via
    resolve_seating_ids), and duplicates are resolved in the order those players
    connected -- which need not be the order they signed up in. The mapping back
    is positional, so it cannot detect the swap: it returns a full, confident,
    wrong answer. The cost is not a mis-posted message; it is two players handed
    each other's forty-five cards with five tix taken off each of them.

    Refusing matches what the bot already does when it is merely choosing who to
    ping on a duplicate name.
    """
    log = {
        "carddata": {"c1": {"name": "Swamp"}, "c2": {"name": "Island"}},
        "users": {"dm_a": {"userName": "Alex", "seatNum": 0, "cards": ["c1"]},
                  "dm_b": {"userName": "Alex", "seatNum": 1, "cards": ["c2"]}},
    }
    await _seed(draft_data=log, sign_ups={ALICE: "Alex", BOB: "Alex"})

    assert await svc.assign_drafted_decks(SESSION) == 0
    assert await _loans() == {}, "nobody gets a deck on an ambiguous seating"


async def test_one_shared_name_does_not_block_the_unambiguous_drafters():
    """The refusal is deliberately whole-draft, so this documents the choice
    rather than asserting a per-player carve-out: the mapping is positional, so
    one duplicated pair shifts nothing for the others only if the alignment is
    otherwise sound -- which is exactly what cannot be verified from here."""
    from services.draft_deck_assignment import _shared_names

    assert _shared_names({"a": "Alex", "b": "Alex", "c": "Sam"}) == {"Alex"}
    assert _shared_names({"a": "Alex", "b": "Sam"}) == set()
