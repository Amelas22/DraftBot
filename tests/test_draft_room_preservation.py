"""When may the bot replace a draft's Draftmancer room?

Replacing is destructive and irreversible. `regenerate_draft_session` abandons
the room and opens an empty one, and the room is the only copy of the draft log
-- Draftmancer keeps it for about 28 minutes and no longer. So the decision is
not "is it safe to keep the room", which is always yes; it is "do we have
positive evidence the room is replaceable", and everything else must preserve.

`eastern-paladin-84` (2026-08-28) lost its log because the code defaulted the
other way. The bot restarted as the draft ended, rebuilt a manager with
`drafting` False, read `session_stage == 'pairings'` -- a stage the guard did
not recognise -- and regenerated, one second after seeing all eight players
still sitting in the room it threw away.
"""
import pytest
from datetime import datetime as _dt
from unittest.mock import AsyncMock, MagicMock, patch

from models.draft_session import DraftSession
from conftest import make_manager, seed_session

pytestmark = pytest.mark.asyncio

# make_manager pins session_id="s", so the seeded row has to match it.
SESSION_ID = "s"


async def _preserve(stage, drafting=False):
    """Whether a fresh manager would preserve the room for a session at `stage`."""
    await seed_session(session_id=SESSION_ID, stage=stage)
    manager = make_manager()
    manager.drafting = drafting
    return await manager.must_preserve_draft_room()


# --- the durable predicate --------------------------------------------------

async def test_a_drafted_session_preserves_its_room(test_db):
    """The regression. 'pairings' means the draft already happened.

    A manager rebuilt after a restart has `drafting` False, so the stored stage
    is the only evidence left that anyone drafted.
    """
    assert await _preserve("pairings") is True


async def test_a_session_with_teams_preserves_its_room(test_db):
    """Teams formed means personalised room links have gone out to players."""
    assert await _preserve("teams") is True


async def test_an_active_draft_preserves_its_room(test_db):
    """The in-memory flag still short-circuits, whatever the stored stage."""
    assert await _preserve(None, drafting=True) is True


async def test_a_queued_session_may_have_its_room_replaced(test_db):
    """The permission this guard exists to grant.

    Before teams exist nobody holds a link, so a broken room should be replaced
    rather than stranding the draft. Without this the fix would trade one bug
    for a worse one.
    """
    assert await _preserve(None) is False


async def test_a_missing_session_preserves_its_room(test_db):
    """No evidence is not evidence of safety.

    A missing row returned False, and callers read False as "regenerate" -- so
    the ambiguous case authorised the destructive branch.
    """
    manager = make_manager()
    manager.session_id = "nonexistent"
    assert await manager.must_preserve_draft_room() is True


async def test_a_failed_lookup_preserves_its_room_and_warns(test_db):
    """A database blip must not read as permission to destroy the room."""
    await seed_session(session_id=SESSION_ID, stage="pairings")
    manager = make_manager()
    manager.logger = MagicMock()

    with patch("services.draft_setup_manager.db_session",
               side_effect=RuntimeError("database is locked")):
        assert await manager.must_preserve_draft_room() is True
    assert manager.logger.warning.called, (
        "preserving because the session could not be read is worth a warning; "
        "otherwise it is indistinguishable from a normal preserve"
    )


# --- the call site that can destroy the room --------------------------------

async def _import_cube_with_ownership_error(stage):
    """Drive import_cube's ownership branch. Returns True if it regenerated."""
    await seed_session(session_id=SESSION_ID, stage=stage)
    manager = make_manager()
    manager.drafting = False
    manager.session_users = []
    manager.users_count = 0

    # Only the first importCube fails: import_cube is wrapped in a backoff
    # retry, the decision under test is made on that first reply, and letting
    # the retry succeed keeps the test instant instead of minutes of sleeping.
    seen = []

    async def emit(event, data=None, callback=None):
        if event == "importCube" and callback:
            if seen:
                callback({"ok": True})
            else:
                seen.append(event)
                callback({"error": {"title": "Unautorized",
                                    "text": "Must be session owner."}})
        return True

    manager.socket_client.emit = AsyncMock(side_effect=emit)
    regenerate = AsyncMock(return_value=False)
    # import_cube is wrapped in an exponential backoff that sleeps for real
    # before each retry -- ~2.4s per test, and the decision under test is made
    # on the first reply.
    with patch("services.draft_setup_manager.asyncio.sleep", AsyncMock()), \
         patch.object(type(manager), "regenerate_draft_session", regenerate), \
         patch.object(type(manager), "_handle_ownership_loss_with_pairings", AsyncMock()), \
         patch.object(type(manager), "_get_draft_channel", AsyncMock(return_value=None)), \
         patch.object(type(manager), "_cleanup_and_disconnect", AsyncMock()):
        # import_cube catches its own StopRetryException in a broad `except`
        # before the backoff decorator can see it, so nothing escapes here. The
        # bare try stays only so this test does not depend on that staying true.
        try:
            await manager.import_cube()
        except Exception:
            pass
    return regenerate.await_count > 0


async def test_a_drafted_session_is_preserved_even_before_its_users_arrive(test_db):
    """`session_users` is filled by a socket event that may not have landed yet.

    import_cube gated preservation on users being visible, so a manager that
    reached the ownership check first saw an empty room and regenerated a
    committed draft -- the same destruction, reached without consulting the
    stage at all.
    """
    assert await _import_cube_with_ownership_error("pairings") is False


async def test_a_queued_session_with_no_users_is_still_replaced(test_db):
    """The permission survives at the call site too, not just in the predicate."""
    assert await _import_cube_with_ownership_error(None) is True


async def test_a_finished_draft_preserves_its_room(test_db):
    """`completed` is written when the deciding result is reported.

    That can land before the log is captured, and the manager is still alive --
    nothing disconnects it on draft end. A whitelist of (teams, pairings) put
    this stage back on the replaceable side, which is the same shape as the bug
    this module exists to prevent, one stage further along.
    """
    assert await _preserve("completed") is True


async def test_an_abandoned_draft_preserves_its_room(test_db):
    """Abandoning a draft is not a reason to destroy the room it happened in."""
    assert await _preserve("abandoned") is True


async def test_an_unrecognised_stage_preserves_its_room(test_db):
    """A stage nobody has taught this predicate about must not mean "destroy".

    Every version of this bug has been an unrecognised value reading as
    permission, so the predicate is a blacklist of one: only the queue is
    replaceable.
    """
    assert await _preserve("some-future-stage") is True


async def test_a_manager_that_saw_the_draft_end_preserves_its_room(test_db):
    """`draft_finished` is set by the endDraft event.

    A manager that watched the draft finish knows the room is committed even if
    the stored stage has not caught up yet.
    """
    await seed_session(session_id=SESSION_ID, stage=None)
    manager = make_manager()
    manager.drafting = False
    manager.draft_finished = True
    assert await manager.must_preserve_draft_room() is True


# --- the write itself -------------------------------------------------------
#
# A guard that can be raced is not a guard. The decision to replace is read in
# one transaction and acted on in another, with network round trips in between,
# so team creation can commit `session_stage='teams'` in the gap. The write has
# to re-check what the decision was based on.

async def _regenerate_after(stage_changes_to):
    """Regenerate a queued session whose stage changes before the write lands."""
    from sqlalchemy import update as sa_update
    from database.db_session import db_session

    await seed_session(session_id=SESSION_ID, stage=None, draft_id="OLDROOM")
    manager = make_manager(draft_id="OLDROOM")
    # make_manager's socket is a MagicMock; regeneration awaits disconnect().
    manager.socket_client.disconnect = AsyncMock()

    # The stage moves after the decision was made and before the write lands.
    async with db_session() as session:
        await session.execute(sa_update(DraftSession)
                              .where(DraftSession.session_id == SESSION_ID)
                              .values(session_stage=stage_changes_to))

    ok = await manager.regenerate_draft_session()
    return ok, (await DraftSession.get_by_session_id(SESSION_ID)).draft_id


async def test_regeneration_replaces_the_room_of_a_still_queued_session(test_db):
    """The normal path still works: nothing committed, so a new room is fine."""
    ok, draft_id = await _regenerate_after(None)
    assert ok is True
    assert draft_id != "OLDROOM"


async def test_regeneration_aborts_if_the_draft_committed_first(test_db):
    """The race the incident is made of.

    Team creation can commit `session_stage='teams'` between the decision and
    the write. Without a compare-and-swap the stale decision still overwrites
    draft_id, abandoning a room that is now committed -- which is the original
    failure with a different trigger.
    """
    ok, draft_id = await _regenerate_after("teams")
    assert ok is False, "a stale replaceable decision must not overwrite the room"
    assert draft_id == "OLDROOM", "the committed room's id was overwritten anyway"


# --- creating rooms is idempotent ------------------------------------------
#
# Both callers -- the ownership-loss handler and /mutiny -- reach the shared
# wrapper for drafts that have already reached 'pairings', which is exactly when
# the rooms exist. create_rooms_pairings refuses to run twice and returns False,
# which is indistinguishable from a real failure, so the wrapper used to announce
# "could not create rooms automatically, use the button" into a channel whose
# button was deleted when the rooms were made. Testing the wrapper rather than
# one caller is what makes this cover both of them.

async def _fallback_with_rooms(rooms_created_at, stage="pairings", victory=None):
    """Run the shared wrapper against a session with/without rooms."""
    import services.draft_setup_manager as dsm

    await seed_session(session_id=SESSION_ID, stage=stage, victory=victory,
                       rooms_created_at=rooms_created_at)
    channel = AsyncMock()
    inner = AsyncMock(return_value=False)   # what create_rooms_pairings does twice
    view = MagicMock()
    view.create_rooms_pairings = inner
    with patch.dict("sys.modules", {"views": MagicMock(PersistentView=view)}):
        ok = await dsm.create_rooms_and_pairings_with_fallback(
            MagicMock(), MagicMock(), channel, SESSION_ID)
    said = " ".join(str(c.args[0]) for c in channel.send.await_args_list)
    return ok, inner.called, said


async def test_existing_rooms_are_not_created_again(test_db):
    """Already created is success, and says nothing."""
    ok, tried, said = await _fallback_with_rooms(_dt.now())
    assert ok is True
    assert not tried, "create_rooms_pairings was called for rooms that already exist"
    assert said == "", f"it announced something about rooms that already exist: {said!r}"


async def test_rooms_are_still_created_when_they_do_not_exist(test_db):
    """The wrapper must still do its job, or this trades a bug for a dead path."""
    ok, tried, said = await _fallback_with_rooms(None)
    assert tried, "create_rooms_pairings was never called for a draft with no rooms"
    assert ok is False, "a genuine failure must still report failure"
    assert "Could not create rooms" in said



async def test_an_abandoned_draft_is_not_given_rooms(test_db):
    """/abandon voids every match result and queues the draft's channels for
    deletion, and it leaves rooms_created_at alone. A manager can still be
    registered when that happens, and both callers of this wrapper reach it for
    any session that has left the queue -- so with no terminal-state check,
    /mutiny on an abandoned draft builds rooms and pairings for a record that
    was just voided.
    """
    ok, tried, said = await _fallback_with_rooms(None, stage="abandoned")
    assert not tried, "created rooms for an abandoned draft"
    assert ok is True, "nothing left to do is not a failure to report"
    assert said == "", f"it announced something about an abandoned draft: {said!r}"


async def test_a_played_out_draft_is_not_given_rooms(test_db):
    """Completion has to be read from the victory message, not the stage. Most
    fully played drafts stop at 'pairings' -- 2472 rows against 758 -- so a
    guard written as `session_stage == 'completed'` lets the common case
    straight through and rebuilds rooms for a draft that is over.
    """
    ok, tried, said = await _fallback_with_rooms(None, stage="pairings", victory=987)
    assert not tried, "created rooms for a draft that had already finished"
    assert ok is True, "nothing left to do is not a failure to report"
    assert said == "", f"it announced something about a finished draft: {said!r}"
