"""A seated player who leaves and comes back must get re-seated.

From a live incident (2026-08-14 22:54, session 1038898216265076777-1786760219):
the bot set the seating order for six, one of them left a second later — before the
ready check had started — and rejoined four seconds after that. The rejoin was
noticed ("Reached expected user count or need to reset seating!") but nothing
happened, because check_session_stage_and_organize returns immediately while
seating_order_set is True. Draftmancer had dropped the leaver from the seating, so
the room was mis-seated with no way back and a human had to run /mutiny.

invalidate_ready_check already clears the flag, but only fires for a leave during an
ACTIVE ready check. This player left in the gap between the seating being set and
the check starting, so nothing covered it.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.draft_setup_manager import DraftSetupManager

SEATS = ["Adham", "Birb", "LSV", "gregg", "Mack", "Mark R"]


def _users(names):
    """A Draftmancer sessionUsers payload, bot included as it is in the real feed."""
    people = [{"userID": f"id-{n}", "userName": n} for n in names]
    return people + [{"userID": "id-bot", "userName": "DraftBot"}]


def _manager(sign_ups):
    mgr = DraftSetupManager(session_id="s", draft_id="d", cube_id="c", guild_id="g")
    mgr.socket_client = MagicMock()
    mgr.socket_client.connected = True
    mgr.socket_client.emit = AsyncMock(return_value=True)
    mgr.expected_user_count = len(sign_ups)
    mgr.desired_seating_order = list(sign_ups)
    mgr.draft_channel_id = "chan"
    # Discord I/O is not what this is about.
    mgr.update_status_message_after_user_change = AsyncMock()
    return mgr


def _db_session(sign_ups):
    row = MagicMock()
    row.session_stage = "teams"
    row.sign_ups = {f"discord-{n}": n for n in sign_ups}
    row.status_message_id = "msg"
    row.draft_channel_id = "chan"
    return row


@pytest.mark.asyncio
async def test_a_seated_player_who_leaves_and_returns_is_reseated():
    mgr = _manager(SEATS)
    attempts = []

    async def fake_attempt(desired_seating_order):
        attempts.append(list(desired_seating_order))
        # what a successful attempt does, including recording who it seated
        mgr.seating_order_set = True
        mgr.seated_user_ids = {f"id-{n}" for n in desired_seating_order}

    mgr.attempt_seating_order = fake_attempt

    with patch("services.draft_setup_manager.DraftSession.get_by_session_id",
               new=AsyncMock(return_value=_db_session(SEATS))):
        # everyone arrives -> the bot seats the table
        await mgr._on_session_users(_users(SEATS))
        assert len(attempts) == 1, "the bot should seat the table once everyone is in"
        assert mgr.seating_order_set is True

        # gregg drops out a second later, BEFORE any ready check is running
        assert mgr.ready_check_active is False
        await mgr._on_session_users(_users([n for n in SEATS if n != "gregg"]))

        # ...and comes back
        await mgr._on_session_users(_users(SEATS))

    assert len(attempts) == 2, (
        "after a seated player left and rejoined the seating is stale in Draftmancer, "
        "so the bot must set it again — otherwise the only way out is /mutiny"
    )


@pytest.mark.asyncio
async def test_a_player_leaving_after_seating_clears_the_seated_flag():
    """The narrower contract the recovery depends on: once a seated player is gone,
    the bot must stop believing the seating it sent is still valid."""
    mgr = _manager(SEATS)
    # the state the bot is in once it has seated everyone
    mgr.session_users = _users(SEATS)
    mgr.users_count = len(SEATS)
    mgr.seating_order_set = True
    mgr.seated_user_ids = {f"id-{n}" for n in SEATS}

    with patch("services.draft_setup_manager.DraftSession.get_by_session_id",
               new=AsyncMock(return_value=_db_session(SEATS))):
        await mgr._on_session_users(_users([n for n in SEATS if n != "gregg"]))

    assert mgr.seating_order_set is False


@pytest.mark.asyncio
async def test_a_seated_player_swapped_for_a_newcomer_is_noticed():
    """The count is a poor proxy for the thing that matters.

    One seated player leaving while somebody else arrives in the same payload leaves
    the count untouched and the seating exactly as stale — Draftmancer has dropped
    the leaver from the order either way. Detecting on "the count fell" would miss
    it and reproduce the original incident with no way back.
    """
    mgr = _manager(SEATS)
    mgr.session_users = _users(SEATS)
    mgr.users_count = len(SEATS)
    mgr.seating_order_set = True
    mgr.seated_user_ids = {f"id-{n}" for n in SEATS}

    swapped = [n for n in SEATS if n != "gregg"] + ["latecomer"]
    assert len(swapped) == len(SEATS), "the point of this test is that the count holds"

    with patch("services.draft_setup_manager.DraftSession.get_by_session_id",
               new=AsyncMock(return_value=_db_session(SEATS))):
        await mgr._on_session_users(_users(swapped))

    assert mgr.seating_order_set is False


@pytest.mark.asyncio
async def test_an_unseated_arrival_does_not_reset_anything():
    """A seventh person wandering in must not invalidate a good seating.

    Spies on the re-seat rather than reading the flag afterwards: an implementation
    that cleared it and immediately re-seated would leave the flag True again, so the
    flag alone cannot tell "never touched" from "thrown away and rebuilt".
    """
    mgr = _manager(SEATS)
    mgr.session_users = _users(SEATS + ["wanderer", "loiterer"])
    mgr.users_count = len(SEATS) + 2
    mgr.seating_order_set = True
    mgr.seated_user_ids = {f"id-{n}" for n in SEATS}

    attempts = []

    async def fake_attempt(desired_seating_order):
        attempts.append(list(desired_seating_order))

    mgr.attempt_seating_order = fake_attempt

    with patch("services.draft_setup_manager.DraftSession.get_by_session_id",
               new=AsyncMock(return_value=_db_session(SEATS))):
        # a hanger-on leaves: the count drops, but nobody we seated has gone
        await mgr._on_session_users(_users(SEATS + ["wanderer"]))

    assert mgr.seating_order_set is True
    assert not attempts, "a good seating must not be re-sent"


@pytest.mark.asyncio
async def test_a_disconnect_during_the_draft_does_not_disturb_the_seating():
    """The dangerous case. Nothing in check_session_stage_and_organize checks
    self.drafting — seating_order_set IS the latch that stops the seating being
    touched again — so clearing it mid-draft would let a reconnect re-emit
    setSeating into a live draft. Worse than the bug this branch fixes."""
    mgr = _manager(SEATS)
    mgr.session_users = _users(SEATS)
    mgr.users_count = len(SEATS)
    mgr.seating_order_set = True
    mgr.seated_user_ids = {f"id-{n}" for n in SEATS}
    mgr.drafting = True

    with patch("services.draft_setup_manager.DraftSession.get_by_session_id",
               new=AsyncMock(return_value=_db_session(SEATS))):
        await mgr._on_session_users(_users([n for n in SEATS if n != "gregg"]))

    assert mgr.seating_order_set is True


@pytest.mark.asyncio
async def test_a_paused_draft_is_left_alone_too():
    mgr = _manager(SEATS)
    mgr.session_users = _users(SEATS)
    mgr.users_count = len(SEATS)
    mgr.seating_order_set = True
    mgr.seated_user_ids = {f"id-{n}" for n in SEATS}
    mgr.draftPaused = True

    with patch("services.draft_setup_manager.DraftSession.get_by_session_id",
               new=AsyncMock(return_value=_db_session(SEATS))):
        await mgr._on_session_users(_users([n for n in SEATS if n != "gregg"]))

    assert mgr.seating_order_set is True


@pytest.mark.asyncio
async def test_one_of_two_players_sharing_a_display_name_leaving_is_noticed():
    """resolve_seating_ids exists because two players can share a display name.
    Comparing names would see "Sam" still present and miss the departure entirely,
    leaving the table mis-seated — so the comparison is by Draftmancer id.

    Asserts a re-seat was ATTEMPTED rather than that the flag stayed False: once the
    departure is noticed the recovery runs immediately and sets the flag again, which
    is the whole point.
    """
    mgr = _manager(SEATS)
    both_sams = [{"userID": "id-sam-1", "userName": "Sam"},
                 {"userID": "id-sam-2", "userName": "Sam"},
                 {"userID": "id-bot", "userName": "DraftBot"}]
    mgr.session_users = both_sams
    mgr.users_count = 2
    mgr.seating_order_set = True
    mgr.seated_user_ids = {"id-sam-1", "id-sam-2"}
    # the seating really was two people who display identically — this is what a
    # name-based comparison cannot tell apart
    mgr.desired_seating_order = ["Sam", "Sam"]
    mgr.expected_user_count = 2

    attempts = []

    async def fake_attempt(desired_seating_order):
        attempts.append(list(desired_seating_order))

    mgr.attempt_seating_order = fake_attempt

    with patch("services.draft_setup_manager.DraftSession.get_by_session_id",
               new=AsyncMock(return_value=_db_session(["Sam"]))):
        # one Sam leaves; a name-based check would still see "Sam" and do nothing
        await mgr._on_session_users([both_sams[0], both_sams[2]])

    assert attempts, "the departure of one duplicate-named player must be noticed"


@pytest.mark.asyncio
async def test_flapping_cannot_reseat_without_limit():
    """seating_attempts is the cap on re-seating for the whole session. Resetting it
    on every departure would let someone leaving and rejoining drive setSeating
    forever."""
    mgr = _manager(SEATS)
    mgr.session_users = _users(SEATS)
    mgr.users_count = len(SEATS)
    mgr.seating_order_set = True
    mgr.seated_user_ids = {f"id-{n}" for n in SEATS}
    mgr.seating_attempts = 3

    with patch("services.draft_setup_manager.DraftSession.get_by_session_id",
               new=AsyncMock(return_value=_db_session(SEATS))):
        await mgr._on_session_users(_users([n for n in SEATS if n != "gregg"]))

    assert mgr.seating_attempts == 3, "the attempt cap must survive a departure"
