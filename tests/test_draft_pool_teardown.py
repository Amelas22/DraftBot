"""Every path that ends a draft must empty its pool.

Half of all staked drafts never fire, so these paths run more often than
settlement does. Money left in prize:draft:<id> after the draft is gone has no
owner and no later process that will ever attribute it.
"""
import pytest
import pytest_asyncio

from conftest import seed_session

from services import draft_pool_service as pool
from services import wallet_service



@pytest_asyncio.fixture(autouse=True)
async def _an_open_queue(test_db):
    """Every charge in production happens against a draft that exists.

    set_entry now refuses to charge into a session with no row -- a component
    left open after its draft was deleted used to read as an open queue and
    strand the money in a holder nothing would settle. These tests charged
    against ids that were never seeded, so they were exercising exactly that
    hole; the row is what production would have.
    """
    await seed_session("s1", guild="g", stype="staked", stage=None,
                       teams=(["p1"], ["b1"]))


@pytest.mark.asyncio
async def test_releasing_returns_every_entry(test_db):
    for p in ("p1", "p2"):
        await wallet_service.adjust("g", p, 100, "seed", "test")
        await pool.set_entry("g", "s1", p, 40)

    result = await pool.release_draft_pool("g", "s1", "cancelled")

    assert result["refunded"] == {"p1": 40, "p2": 40}
    assert await pool.pool_balance("g", "s1") == 0
    assert await wallet_service.get_balance("g", "p1") == 100


@pytest.mark.asyncio
async def test_releasing_a_draft_with_no_pool_is_harmless(test_db):
    """Most drafts are not staked; every teardown path calls this regardless."""
    assert await pool.release_draft_pool("g", "never", "cancelled") == {"refunded": {}}


@pytest.mark.asyncio
async def test_releasing_twice_refunds_once(test_db):
    await wallet_service.adjust("g", "p1", 100, "seed", "test")
    await pool.set_entry("g", "s1", "p1", 40)

    await pool.release_draft_pool("g", "s1", "cancelled")
    await pool.release_draft_pool("g", "s1", "cancelled")

    assert await wallet_service.get_balance("g", "p1") == 100


@pytest.mark.asyncio
async def test_a_released_pool_settles_nothing_afterwards(test_db):
    """An abandoned draft that later reaches the victory path must not pay out
    money it has already given back."""
    await wallet_service.adjust("g", "p1", 100, "seed", "test")
    await pool.set_entry("g", "s1", "p1", 40)
    await pool.release_draft_pool("g", "s1", "abandoned")

    assert await pool.settle_pool("g", "s1", ["p1"]) == {"paid": {}}
    assert await wallet_service.get_balance("g", "p1") == 100


# --- the hooks: wired, and now actually asserted ------------------------------
# These read the source of the call sites. That is deliberate: what they pin is
# ORDER and PRESENCE at points whose real execution needs a live Discord client,
# and the round-trip e2e drives those same paths for real.

def _source(fn):
    import inspect
    return inspect.getsource(fn)


def test_forming_teams_closes_the_book():
    """Matching has to be CALLED, not merely correct. Teams do not exist until
    seating for a random draft, so the stage write is the earliest point it can
    run -- and the last bot-side step before players draft, so the latest it may."""
    from services.team_creator import create_and_display_teams

    src = _source(create_and_display_teams)
    stage_at = src.find("session.session_stage = 'teams'")
    match_at = src.find("match_pool(")
    assert match_at != -1, "team creation never closes the book"
    assert stage_at != -1, "the stage write moved; re-check this ordering assertion"
    assert stage_at < match_at, (
        "matching runs BEFORE the draft is marked at teams, so it may fire for a "
        "session that never reaches the table")


def test_the_queue_cancel_refunds_before_deleting_the_row():
    """Ordering IS the test. The pool is keyed to session_id and this path
    deletes the DraftSession row, so a refund running afterwards has nothing to
    key to and the money is stranded with no owner."""
    from views import CancelConfirmationView

    src = _source(CancelConfirmationView.confirm_button)
    release_at = src.find("release_draft_pool")
    delete_at = src.find("db_session.delete(session)")
    assert release_at != -1, "the queue cancel does not release the pool at all"
    assert delete_at != -1, "the delete moved; re-check this ordering assertion"
    assert release_at < delete_at, (
        "the pool is released AFTER the DraftSession row is deleted -- by then "
        "there is nothing to key the refund to")


def test_abandoning_a_draft_releases_its_pool():
    """An abandoned draft voids its results, so it pays nobody -- and like a
    draw, paying nobody has to mean refunding everybody."""
    from cogs.draft_control import abandon_draft_session

    src = _source(abandon_draft_session)
    assert "release_draft_pool" in src, "abandon leaves the pool funded"
    assert '"abandoned"' in src, "the refund is not tagged as an abandon"


@pytest.mark.asyncio
async def test_a_draw_releases_its_pool_from_the_victory_path(test_db):
    """Settlement is victory-only, so the draw branch is the only thing between
    a drawn staked draft and a permanently funded holder: the draft is marked
    completed either way, and nothing revisits it afterwards."""
    from sqlalchemy import update

    from models.match import MatchResult
    from session import AsyncSessionLocal, DraftSession
    from utils import settle_decided_draft

    await seed_session("draw1", guild="g", stype="staked", stage=None,
                       teams=(["p1"], ["p2"]))
    for player in ("p1", "p2"):
        await wallet_service.adjust("g", player, 500, f"seed-{player}", "t")
        await pool.set_entry("g", "draw1", player, 40)
    await pool.match_pool("g", "draw1", ["p1"], ["p2"])
    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(update(DraftSession)
                             .where(DraftSession.session_id == "draw1")
                             .values(session_stage="teams", match_counter=3))
            db.add(MatchResult(session_id="draw1", match_number=1,
                               player1_id="p1", player1_wins=2,
                               player2_id="p2", player2_wins=0,
                               winner_id="p1", pairing_message_id="0"))
            db.add(MatchResult(session_id="draw1", match_number=2,
                               player1_id="p2", player1_wins=2,
                               player2_id="p1", player2_wins=0,
                               winner_id="p2", pairing_message_id="0"))

    await settle_decided_draft("draw1")

    assert await pool.pool_balance("g", "draw1") == 0, (
        "a drawn draft never empties its pool -- every entry stays in the "
        "holder on a draft already marked completed")
    balances = await wallet_service.balances_for("g", ["p1", "p2"])
    assert balances == {"p1": 500, "p2": 500}, "a draw did not return every entry"


def test_a_queue_that_never_fills_gives_the_money_back():
    """The commonest end for a staked draft -- 1,207 of 2,398 historically, the
    single largest number in the design. The row is reaped and the pool is keyed
    to its session_id, so anything still held becomes unattributable."""
    from utils import cleanup_sessions_task

    src = _source(cleanup_sessions_task)
    assert "release_draft_pool" in src, (
        "the cleanup task reaps stale queues without returning their entries")
    assert '"expired"' in src


def test_a_scrapped_draft_gives_the_money_back():
    """Scrapped means nobody played it, so nobody can be paid for it."""
    import inspect

    from cogs.draft_control import DraftControlCog

    src = inspect.getsource(DraftControlCog)
    assert '"scrapped"' in src, "a scrapped draft leaves its pool funded"


def test_a_scrap_refunds_only_after_draftmancer_confirms():
    """A failed stopDraft leaves the draft running, and the code says so. Money
    must not have been handed back before that point: refunding a draft players
    are still sitting in means the pool is empty when it later completes."""
    import inspect

    from cogs.draft_control import DraftControlCog

    src = inspect.getsource(DraftControlCog)
    emit_at = src.find("emit('stopDraft')")
    release_at = src.find('"scrapped"')
    assert emit_at != -1 and release_at != -1
    assert emit_at < release_at, (
        "the pool is released before stopDraft is confirmed, so a draft that "
        "could not be reached is refunded while still being played")


@pytest.mark.asyncio
async def test_a_restart_does_not_hand_a_pool_draft_a_settle_debts_button(test_db):
    """The live victory path gates that button on a pre-conversion draft; the
    restart path did not, so every bot restart put it back on pool drafts for
    the previous week. It invites a player to pay a debt the pool means nobody
    ever incurs.
    """
    import ast
    import inspect
    import textwrap

    import utils

    src = textwrap.dedent(inspect.getsource(utils.re_register_views))
    fn = ast.parse(src).body[0]
    views = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "SettleDebtsView"]
    assert views, "the restart path no longer offers the button; re-check this"
    assert "get_draft_debtors" in src, (
        "the restart path attaches SettleDebtsView without asking whether the "
        "draft made anyone a debtor, so a pool draft gets the button back on "
        "every restart")
