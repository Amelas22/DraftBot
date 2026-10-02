"""A draft whose prize pool cannot be settled goes back to sign-ups.

match_pool runs after the team-creation transaction commits, so a failure
there used to leave a draft at stage 'teams' with its pool unmatched -- and
announced anyway, quoting what players had declared. match_pool is all or
nothing, so a failure has moved no money; team creation now puts the draft back
exactly as it was before the attempt, says so, and nothing is announced.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.team_creator as team_creator
from conftest import seed_session
from services import draft_pool_service as pool
from services import wallet_service
from session import get_draft_session

PLAYERS = {"p1": "Ann", "p2": "Ben", "p3": "Cat", "p4": "Dan"}
ENTRIES = {"p1": 100, "p2": 50, "p3": 30, "p4": 20}


async def _staked_queue():
    expiry = datetime.now() + timedelta(minutes=90)
    await seed_session("s1", guild="1", stype="staked", stage=None,
                       sign_ups=dict(PLAYERS), deletion_time=expiry)
    for player, amount in ENTRIES.items():
        await wallet_service.adjust("1", player, 1000, "seed", "test")
        await pool.set_entry("1", "s1", player, amount)
    return expiry


def _interaction():
    interaction = MagicMock()
    interaction.guild_id = 1
    interaction.message = SimpleNamespace(id=0)
    interaction.followup.send = AsyncMock()
    interaction.followup.edit_message = AsyncMock()
    interaction.channel.send = AsyncMock()
    return interaction


async def _create_teams_with_a_failing_pool(interaction):
    view = MagicMock()
    view.session_type = "staked"
    view.children = []
    with patch.object(team_creator, "match_pool",
                      AsyncMock(side_effect=pool.PoolNotSettled("refused"))), \
         patch("ready_check.ReadyCheckSession.cleanup", AsyncMock()):
        return await team_creator.create_and_display_teams(
            MagicMock(), "s1", interaction, view)


@pytest.mark.asyncio
async def test_a_draft_whose_pool_cannot_settle_returns_to_sign_ups(test_db):
    await _staked_queue()
    columns = team_creator._UNWIND_COLUMNS
    as_was = {c: getattr(await get_draft_session("s1"), c) for c in columns}
    entries_before = await pool.contributions("1", "s1")

    created = await _create_teams_with_a_failing_pool(_interaction())

    assert created is False
    row = await get_draft_session("s1")
    assert {c: getattr(row, c) for c in columns} == as_was
    assert row.session_stage is None and row.team_a is None
    assert list(row.sign_ups) == list(PLAYERS), "the sign-up order was left shuffled"
    assert await pool.contributions("1", "s1") == entries_before


@pytest.mark.asyncio
async def test_the_unwound_draft_is_explained_and_never_announced(test_db):
    await _staked_queue()
    interaction = _interaction()

    await _create_teams_with_a_failing_pool(interaction)

    said = " ".join(str(c.args[0]) for c in interaction.followup.send.await_args_list)
    assert "couldn't be settled" in said and "Create Teams" in said
    interaction.followup.edit_message.assert_not_awaited()
    interaction.channel.send.assert_not_awaited()


# --- a crash between committing the teams and matching the pool ------------
#
# No except clause sees a process dying. With match_pool all or nothing, what a
# crash leaves is a draft at 'teams' whose pool was never touched -- its sides
# still unequal -- and nothing announced. Startup puts those back too.

async def _half_made(session_id, stype="staked", teams=(["p1", "p3"], ["p2", "p4"]),
                     entry_fee=None):
    """Funded as an open queue, then moved to 'teams' -- the order it happens in."""
    from sqlalchemy import update
    from database.db_session import AsyncSessionLocal
    from models.draft_session import DraftSession

    await seed_session(session_id, guild="1", stype=stype, stage=None,
                       sign_ups=dict(PLAYERS), teams=teams,
                       start=datetime.now() - timedelta(minutes=10))
    for player, amount in ENTRIES.items():
        await wallet_service.adjust("1", player, 1000, "seed", "test")
        await pool.set_entry("1", session_id, player, amount)
    async with AsyncSessionLocal() as s:
        await s.execute(update(DraftSession).where(
            DraftSession.session_id == session_id).values(
                session_stage="teams", entry_fee=entry_fee, draft_channel_id="123"))
        await s.commit()


def _bot():
    bot = MagicMock()
    bot.get_channel.return_value.send = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_startup_returns_a_half_made_draft_to_sign_ups(test_db):
    await _half_made("s1")
    bot = _bot()

    await team_creator.unwind_interrupted_team_creations(bot)

    row = await get_draft_session("s1")
    assert row.session_stage is None
    assert row.team_a is None and row.team_b is None
    assert row.deletion_time > datetime.now(), "the queue would expire on its next sweep"
    assert await pool.contributions("1", "s1") == ENTRIES
    said = str(bot.get_channel.return_value.send.await_args.args[0])
    assert "interrupted" in said and "Create Teams" in said


@pytest.mark.asyncio
async def test_startup_leaves_a_settled_draft_alone(test_db):
    await _half_made("s1")
    await pool.match_pool("1", "s1", ["p1", "p3"], ["p2", "p4"])

    await team_creator.unwind_interrupted_team_creations(_bot())

    assert (await get_draft_session("s1")).session_stage == "teams"


@pytest.mark.asyncio
async def test_startup_keeps_a_premade_drafts_rosters(test_db):
    await _half_made("s1", stype="premade", entry_fee=20)

    await team_creator.unwind_interrupted_team_creations(_bot())

    row = await get_draft_session("s1")
    assert row.session_stage is None
    assert (row.team_a, row.team_b) == (["p1", "p3"], ["p2", "p4"])


def test_startup_unwinds_before_it_reconnects_drafts_in_setup():
    """Once per process, and first: a draft it returns to sign-ups is then a
    draft in setup, which the reconnection after it gives a manager.

    Asserted against the source: on_ready needs a live gateway to run.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path("bot.py").read_text())
    on_ready = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "on_ready")
    calls = sorted((c.lineno, getattr(c.func, "id", None) or getattr(c.func, "attr", None))
                   for c in ast.walk(on_ready) if isinstance(c, ast.Call))
    names = [n for _, n in calls]
    assert "unwind_interrupted_team_creations" in names
    assert names.index("unwind_interrupted_team_creations") < names.index(
        "reconnect_draft_setup_sessions")
    guarded = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
               for n in ast.walk(on_ready) if isinstance(n, ast.If)
               for c in ast.walk(n) if isinstance(c, ast.Call)}
    assert "unwind_interrupted_team_creations" in guarded


@pytest.mark.asyncio
async def test_startup_leaves_a_paid_out_draft_alone(test_db):
    """Once a pool pays out, the winners' net contributions go negative and drop
    out, so its sides LOOK unequal. What a half-made draft has and a settled one
    does not is money still in the pool."""
    await _half_made("s1")
    await pool.match_pool("1", "s1", ["p1", "p3"], ["p2", "p4"])
    await pool.settle_pool("1", "s1", ["p1", "p3"])

    unwound = await team_creator.unwind_interrupted_team_creations(_bot())

    assert unwound == []
    assert (await get_draft_session("s1")).session_stage == "teams"


@pytest.mark.asyncio
async def test_startup_leaves_a_running_draft_alone_after_a_player_is_removed(test_db):
    """Removing a player after teams form refunds their entry and edits the
    rosters, so a running draft's sides go unequal with money still in the pool
    -- the same shape a crash leaves. What tells them apart is that this pool
    was matched, and match_pool records that in the same transaction."""
    from sqlalchemy import update
    from database.db_session import AsyncSessionLocal
    from models.draft_session import DraftSession
    from services.draft_pool_service import entry_in

    await _half_made("s1")
    await pool.match_pool("1", "s1", ["p1", "p3"], ["p2", "p4"])
    async with wallet_service.MONEY_LOCK:
        async with AsyncSessionLocal() as s:
            async with s.begin():
                await entry_in(s, "1", "s1", "p3", 0, "removed")
                await s.execute(update(DraftSession).where(
                    DraftSession.session_id == "s1").values(team_a=["p1"]))
    held = await pool.contributions("1", "s1")
    assert held.get("p1", 0) != held.get("p2", 0) + held.get("p4", 0), \
        "precondition: the removal left the sides unequal"

    unwound = await team_creator.unwind_interrupted_team_creations(_bot())

    assert unwound == []
    assert (await get_draft_session("s1")).session_stage == "teams"


@pytest.mark.asyncio
async def test_matching_records_that_the_pool_was_matched(test_db):
    await _half_made("s1")
    assert (await get_draft_session("s1")).pool_matched_at is None

    await pool.match_pool("1", "s1", ["p1", "p3"], ["p2", "p4"])

    assert (await get_draft_session("s1")).pool_matched_at is not None


@pytest.mark.asyncio
async def test_a_retry_is_told_to_run_the_ready_check_again(test_db):
    """Team creation cleared the ready check, and a staked draft refuses
    Create Teams without one -- so that is the first thing to say."""
    await _staked_queue()
    interaction = _interaction()

    await _create_teams_with_a_failing_pool(interaction)

    said = " ".join(str(c.args[0]) for c in interaction.followup.send.await_args_list)
    assert "ready check" in said.lower()
