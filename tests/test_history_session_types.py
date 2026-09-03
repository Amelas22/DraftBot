"""Which drafts /history counts.

It hand-rolled `or_(random, staked)` and so silently dropped every premade draft
-- which is what a league or tournament match runs as. Those matches move your
rating and your leaderboard position (helpers/skill.RATING_SESSION_TYPES) while
being absent from your own history, which is the contradiction reported.

The fix is not "add premade" but "ask the constant", so the next change to what
counts reaches /history on its own.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import update

from cogs.history_cog import history_drafts
from conftest import seed_session
from models.draft_session import DraftSession
from session import AsyncSessionLocal

GUILD = "g"
P = "p1"


async def _played(session_id, stype, when):
    """A finished draft `P` was in.

    seed_session records the results-channel victory message; /history keys off
    the draft-chat one, so that is set here -- along with teams_start_time,
    which is what the listing orders by.
    """
    await seed_session(session_id, guild=GUILD, stype=stype, sign_ups={P: "Ada"})
    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(
                update(DraftSession)
                .where(DraftSession.session_id == session_id)
                .values(victory_message_id_draft_chat="900", teams_start_time=when))


@pytest.mark.asyncio
async def test_history_counts_the_same_drafts_the_leaderboard_does(test_db):
    base = datetime(2026, 1, 1)
    for i, stype in enumerate(["random", "staked", "premade", "swiss"]):
        await _played(stype, stype, base + timedelta(days=i))

    drafts = await history_drafts(GUILD, P)

    assert sorted(d.session_type for d in drafts) == ["premade", "random", "staked"], (
        "premade is how league and tournament matches are played, and they are "
        "rated -- so they belong in history; swiss is not rated and does not")


@pytest.mark.asyncio
async def test_a_draft_someone_else_played_is_not_in_your_history(test_db):
    await _played("theirs", "premade", datetime(2026, 1, 1))
    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(update(DraftSession)
                             .where(DraftSession.session_id == "theirs")
                             .values(sign_ups={"someone_else": "Brin"}))

    assert await history_drafts(GUILD, P) == []


@pytest.mark.asyncio
async def test_an_unfinished_draft_is_not_in_your_history(test_db):
    """No draft-chat victory message means it never resolved."""
    await seed_session("live", guild=GUILD, stype="premade", sign_ups={P: "Ada"})

    assert await history_drafts(GUILD, P) == []
