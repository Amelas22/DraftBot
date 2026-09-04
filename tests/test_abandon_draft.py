from datetime import datetime

import pytest
from sqlalchemy import select

from models.draft_session import DraftSession
from models.match import MatchResult
from session import AsyncSessionLocal
from cogs.draft_control import abandon_draft_session, AbandonVoteView

# These tests use conftest's `test_db`, which points the process-wide
# AsyncSessionLocal at its temp database, and they hand that same factory in as
# `session_factory`. Both halves matter, because abandon_draft_session writes
# through BOTH routes: the injected factory for the draft's own rows, and the
# global one when release_draft_pool hands the prize pool back.
#
# This file used to define its own `test_db`, shadowing conftest's with a
# fixture that built a factory but never bound the global. The injected half
# worked and the pool half went looking for wallet_tx in a database that had
# none, so the moment an abandoned draft started releasing its pool, the test
# broke -- on a call the fixture had no way to reach.


async def _seed(factory, session_id="s1", stage="pairings"):
    async with factory() as db:
        async with db.begin():
            db.add(DraftSession(
                session_id=session_id, guild_id="g", session_stage=stage,
                sign_ups={"1": "A", "2": "B"},
            ))
            db.add(MatchResult(
                session_id=session_id, match_number=1, player1_id="1", player2_id="2",
                player1_wins=2, player2_wins=1, winner_id="1", result_submitted_at=datetime.now(),
            ))
            db.add(MatchResult(
                session_id=session_id, match_number=2, player1_id="1", player2_id="2",
                winner_id=None,
            ))


@pytest.mark.asyncio
async def test_abandon_voids_matches_and_marks_abandoned(test_db):
    await _seed(AsyncSessionLocal)

    await abandon_draft_session("s1", session_factory=AsyncSessionLocal)

    async with AsyncSessionLocal() as db:
        ds = (await db.execute(
            select(DraftSession).where(DraftSession.session_id == "s1")
        )).scalar_one()
        assert ds.session_stage == "abandoned"
        assert ds.deletion_time is not None

        results = (await db.execute(
            select(MatchResult).where(MatchResult.session_id == "s1")
        )).scalars().all()
        assert results, "expected match rows"
        assert all(r.winner_id is None for r in results)
        assert all(r.player1_wins == 0 and r.player2_wins == 0 for r in results)
        assert all(r.result_submitted_at is None for r in results)


@pytest.mark.asyncio
async def test_abandon_vote_needs_majority_even_participants():
    view = AbandonVoteView("s1", ["1", "2", "3", "4"])
    view.votes = {"1": True, "2": True, "3": None, "4": None}
    passed, yes, total = view.get_vote_result()
    assert (yes, total) == (2, 4)
    assert not passed  # need 3 of 4

    view.votes["3"] = True
    passed, _, _ = view.get_vote_result()
    assert passed


@pytest.mark.asyncio
async def test_abandon_vote_majority_odd_participants():
    view = AbandonVoteView("s1", ["1", "2", "3"])
    view.votes = {"1": True, "2": True, "3": False}
    passed, yes, total = view.get_vote_result()
    assert passed and yes == 2 and total == 3  # need 2 of 3


@pytest.mark.asyncio
async def test_a_draft_that_completed_during_the_vote_is_not_voided(test_db):
    """The guard runs when the command is typed; the voiding happens up to 90
    seconds later, after a vote or an admin's confirm click. In between, the last
    match can be reported and the draft can finish. Re-checking at the mutation is
    the only thing that stops a finished draft losing every result."""
    async with AsyncSessionLocal() as db:
        async with db.begin():
            db.add(DraftSession(
                session_id="s_done", guild_id="g", session_stage="pairings",
                # A played draft usually stays at 'pairings'; the victory message
                # is what marks it finished.
                victory_message_id_draft_chat="12345",
                sign_ups={"1": "A", "2": "B"}))
            db.add(MatchResult(
                session_id="s_done", match_number=1, player1_id="1", player2_id="2",
                player1_wins=2, player2_wins=1, winner_id="1",
                result_submitted_at=datetime.now()))

    voided = await abandon_draft_session("s_done", session_factory=AsyncSessionLocal)

    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(DraftSession).where(DraftSession.session_id == "s_done")
        )).scalar_one()
        match = (await db.execute(
            select(MatchResult).where(MatchResult.session_id == "s_done")
        )).scalar_one()

    assert voided is False, "reported that it abandoned a finished draft"
    assert row.session_stage == "pairings", "marked a finished draft abandoned"
    assert match.winner_id == "1", "voided a finished draft's results"


@pytest.mark.asyncio
async def test_abandoning_a_staked_draft_hands_the_pool_back(test_db):
    """An abandoned draft pays nobody, so every entry goes home.

    This is the call the old shadowing fixture could not reach: release_draft_pool
    opens its own session from the global factory rather than the injected one,
    so it went looking for wallet_tx in a database that had none. Without a
    FUNDED pool the release is a no-op and the path is only exercised trivially.
    """
    from conftest import seed_session
    from services import draft_pool_service as pool
    from services import wallet_service

    players = ["700000000000000001", "700000000000000002"]
    # Entries can only be charged while the queue is open; the draft is moved to
    # 'pairings' afterwards, which is where a draft gets abandoned from.
    await seed_session("s_staked", guild="g", stype="staked", stage=None,
                       teams=(players[:1], players[1:]),
                       sign_ups={p: p for p in players})
    for player in players:
        await wallet_service.adjust("g", player, 500, "seed", "test")
        await pool.set_entry("g", "s_staked", player, 20)
    assert await pool.pool_balance("g", "s_staked") == 40

    # Re-seed rather than hand-patch the stage: seed_session replaces the row, and
    # the pool lives in wallet_tx, so the funding above survives it.
    await seed_session("s_staked", guild="g", stype="staked", stage="pairings",
                       teams=(players[:1], players[1:]),
                       sign_ups={p: p for p in players})

    await abandon_draft_session("s_staked", session_factory=AsyncSessionLocal)

    assert await pool.pool_balance("g", "s_staked") == 0, "the pool was not released"
    for player in players:
        assert await wallet_service.get_balance("g", player) == 500, \
            f"{player} did not get their entry back"
