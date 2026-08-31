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
from unittest.mock import AsyncMock, patch

from services.draft_setup_manager import (
    SESSION_STAGE_PAIRINGS, SESSION_STAGE_TEAMS,
)
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
    assert await _preserve(SESSION_STAGE_PAIRINGS) is True


async def test_a_session_with_teams_preserves_its_room(test_db):
    """Teams formed means personalised room links have gone out to players."""
    assert await _preserve(SESSION_STAGE_TEAMS) is True


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


async def test_an_unreadable_session_preserves_its_room(test_db):
    """No evidence is not evidence of safety.

    A missing row returned False, and callers read False as "regenerate" -- so
    the ambiguous case authorised the destructive branch.
    """
    manager = make_manager()
    manager.session_id = "nonexistent"
    manager.drafting = False
    assert await manager.must_preserve_draft_room() is True


async def test_a_failed_lookup_preserves_its_room_and_warns(test_db):
    """A database blip must not read as permission to destroy the room."""
    await seed_session(session_id=SESSION_ID, stage=SESSION_STAGE_PAIRINGS)
    manager = make_manager()
    manager.drafting = False
    manager.logger = AsyncMock()

    with patch("services.draft_setup_manager.db_session",
               side_effect=RuntimeError("database is locked")):
        assert await manager.must_preserve_draft_room() is True
    assert manager.logger.warning.called, (
        "preserving because the session could not be read is worth a warning; "
        "otherwise it is indistinguishable from a normal preserve"
    )


# --- the call site that can destroy the room --------------------------------

async def _import_cube_with_ownership_error(stage, users):
    """Drive import_cube's ownership branch. Returns True if it regenerated."""
    await seed_session(session_id=SESSION_ID, stage=stage)
    manager = make_manager()
    manager.drafting = False
    manager.session_users = users
    manager.users_count = len(users)

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
    with patch.object(type(manager), "regenerate_draft_session", regenerate), \
         patch.object(type(manager), "_handle_ownership_loss_with_pairings", AsyncMock()), \
         patch.object(type(manager), "_get_draft_channel", AsyncMock(return_value=None)), \
         patch.object(type(manager), "_cleanup_and_disconnect", AsyncMock()):
        try:
            await manager.import_cube()
        except Exception:
            pass  # the preserve branch raises StopRetryException
    return regenerate.await_count > 0


async def test_a_drafted_session_is_preserved_even_before_its_users_arrive(test_db):
    """`session_users` is filled by a socket event that may not have landed yet.

    import_cube gated preservation on users being visible, so a manager that
    reached the ownership check first saw an empty room and regenerated a
    committed draft -- the same destruction, reached without consulting the
    stage at all.
    """
    assert await _import_cube_with_ownership_error(SESSION_STAGE_PAIRINGS, []) is False


async def test_a_queued_session_with_no_users_is_still_replaced(test_db):
    """The permission survives at the call site too, not just in the predicate."""
    assert await _import_cube_with_ownership_error(None, []) is True
