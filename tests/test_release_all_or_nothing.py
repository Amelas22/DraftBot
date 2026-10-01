"""Releasing a draft's pool returns every entry, or none of them.

release_draft_pool refunded each player in a commit of its own and skipped a
refund that was refused. A failure part-way left a pool half-released -- and
the inactive-queue reaper deletes the draft's row right after releasing, so
whatever was still held then had no draft to be returned through. Now the
release is one transaction, like match_pool's: it either returns everything or
raises with the pool untouched.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from conftest import failing_refund, run_until_sleep, seed_session
from services import draft_pool_service as pool
from services import wallet_service
from session import get_draft_session

ENTRIES = {"p1": 30, "p2": 20, "p3": 10}


@pytest_asyncio.fixture(autouse=True)
async def _a_funded_queue(test_db):
    await seed_session("s1", guild="g", stype="staked", stage=None,
                       deletion_time=datetime.now() + timedelta(hours=1))
    for player, amount in ENTRIES.items():
        await wallet_service.adjust("g", player, 100, "seed", "test")
        await pool.set_entry("g", "s1", player, amount)


@pytest.mark.asyncio
@pytest.mark.parametrize("behaviour", ["refused", "raises"])
async def test_a_failed_refund_releases_nothing(behaviour):
    with patch.object(pool, "_refund_in", failing_refund(behaviour=behaviour)):
        with pytest.raises(pool.PoolNotSettled):    # one thing for callers to catch
            await pool.release_draft_pool("g", "s1", "cancelled")

    assert await pool.contributions("g", "s1") == ENTRIES, \
        "part of the pool was released before the failure"


@pytest.mark.asyncio
async def test_the_reaper_keeps_a_queue_whose_pool_it_could_not_release():
    """Deleting the row with money still held strands it; keeping the row lets
    the next pass try again."""
    import utils

    async with pool.db_session() as s:
        from sqlalchemy import update
        from models.draft_session import DraftSession
        await s.execute(update(DraftSession).where(DraftSession.session_id == "s1")
                        .values(deletion_time=datetime.now() - timedelta(minutes=1)))

    bot = MagicMock()
    bot.get_channel.return_value = None
    with patch.object(pool, "_refund_in", failing_refund()):
        await run_until_sleep(utils.cleanup_sessions_task(bot), 600)

    assert await get_draft_session("s1") is not None, "the row went with money still in the pool"
    assert await pool.contributions("g", "s1") == ENTRIES

    await run_until_sleep(utils.cleanup_sessions_task(bot), 600)

    assert await get_draft_session("s1") is None
    assert await pool.pool_balance("g", "s1") == 0


@pytest.mark.asyncio
async def test_a_cancel_whose_entries_cannot_be_returned_changes_nothing():
    """Cancelling announces, stops the draft's manager and deletes the sign-up
    message -- none of which can be taken back. So the entries come back
    FIRST, and if they cannot, the draft is left exactly as it was and the
    canceller is told."""
    import views
    from sqlalchemy import update
    from models.draft_session import DraftSession

    async with pool.db_session() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == "s1")
                        .values(draft_channel_id="123", message_id="1"))

    channel = MagicMock()
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.get_channel.return_value = channel
    view = views.CancelConfirmationView(bot, "s1", "Ann")
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()

    with patch.object(pool, "_refund_in", failing_refund()), \
         patch.object(views.DraftSetupManager, "cancel_for_session", AsyncMock()) as stop:
        await view.confirm_button.callback(interaction)

    assert await get_draft_session("s1") is not None
    assert await pool.contributions("g", "s1") == ENTRIES
    channel.send.assert_not_awaited()
    stop.assert_not_awaited()
    told = " ".join(str(c.args[0]) for c in interaction.followup.send.await_args_list)
    assert "wasn't cancelled" in told


@pytest.mark.asyncio
async def test_an_abandon_whose_entries_cannot_be_returned_says_so():
    """Abandoning commits before it releases, so a failed release cannot be
    undone the way a cancel's is. It must not be announced as if the money had
    gone back: the entries are still held, and somebody has to return them."""
    from sqlalchemy import update
    from models.draft_session import DraftSession
    from cogs.draft_control import AbandonConfirmView

    async with pool.db_session() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == "s1")
                        .values(session_stage="pairings"))
    channel = MagicMock()
    channel.send = AsyncMock()
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    view = AbandonConfirmView("s1", channel)

    with patch.object(pool, "_refund_in", failing_refund()):
        await view.confirm.callback(interaction)

    assert (await get_draft_session("s1")).session_stage == "abandoned"
    assert await pool.contributions("g", "s1") == ENTRIES
    said = " ".join(str(c.args[0]) for c in channel.send.await_args_list)
    assert "couldn't be returned" in said


async def _expire(session_id):
    from sqlalchemy import update
    from models.draft_session import DraftSession
    async with pool.db_session() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == session_id)
                        .values(deletion_time=datetime.now() - timedelta(minutes=1)))


@pytest.mark.asyncio
async def test_one_queue_that_cannot_release_does_not_stop_the_others():
    """The reaper's pass is one transaction. Raising out of it for one queue
    rolled back every other queue, channel and challenge in the pass -- every
    ten minutes, for as long as that one pool kept failing."""
    import utils

    await seed_session("s2", guild="g", stype="staked", stage=None,
                       deletion_time=datetime.now() + timedelta(hours=1))
    await pool.set_entry("g", "s2", "p1", 10)
    await _expire("s1")
    await _expire("s2")
    bot = MagicMock()
    bot.get_channel.return_value = None

    with patch.object(pool, "_refund_in", failing_refund(nth=1, session_id="s1")):
        await run_until_sleep(utils.cleanup_sessions_task(bot), 600)

    assert await get_draft_session("s1") is not None
    assert await get_draft_session("s2") is None, "the healthy queue was held back"
    assert await pool.pool_balance("g", "s2") == 0


def _cancel_view():
    import views
    channel = MagicMock()
    channel.send = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
    bot = MagicMock()
    bot.get_channel.return_value = channel
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return views, views.CancelConfirmationView(bot, "s1", "Ann"), interaction, channel


async def _with_a_channel():
    from sqlalchemy import update
    from models.draft_session import DraftSession
    async with pool.db_session() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == "s1")
                        .values(draft_channel_id="123", message_id="1"))


@pytest.mark.asyncio
async def test_an_entry_made_while_a_cancel_is_in_flight_is_returned_too():
    """Between the first release and the row going, the Join button is still
    live. An entry booked in that window would go down with the row."""
    await _with_a_channel()
    views, view, interaction, _ = _cancel_view()

    async def late_join(_session_id):
        await wallet_service.adjust("g", "late", 100, "seed", "test")
        await pool.set_entry("g", "s1", "late", 40)

    with patch.object(views.DraftSetupManager, "cancel_for_session", side_effect=late_join):
        await view.confirm_button.callback(interaction)

    assert await get_draft_session("s1") is None
    assert await pool.pool_balance("g", "s1") == 0, "the late entry went down with the row"


@pytest.mark.asyncio
async def test_a_cancel_finishes_even_if_discord_fails_partway():
    """Once the entries are back, the queue must not be left open: players
    would sit in a queue whose entries have already been returned."""
    await _with_a_channel()
    views, view, interaction, channel = _cancel_view()
    channel.send = AsyncMock(side_effect=RuntimeError("Discord 503"))

    with patch.object(views.DraftSetupManager, "cancel_for_session", AsyncMock()):
        await view.confirm_button.callback(interaction)

    assert await get_draft_session("s1") is None
    assert await pool.pool_balance("g", "s1") == 0


@pytest.mark.asyncio
async def test_a_draw_whose_pool_cannot_release_still_posts():
    """settle_decided_draft runs inside every match report; raising out of it
    left the draw unposted and every later report failing the same way."""
    import utils

    draft = MagicMock(session_type="staked", entry_fee=None, guild_id="g",
                      team_a=["p1"], team_b=["p2"], match_counter=2, friendly_id="f")
    with patch.object(utils, "get_draft_session", AsyncMock(return_value=draft)), \
         patch.object(utils, "calculate_team_wins", AsyncMock(return_value=(1, 1))), \
         patch.object(utils, "decides_draft", return_value="draw"), \
         patch.object(utils, "settle_draw",
                      AsyncMock(side_effect=pool.PoolNotSettled("refused"))) as draw:
        await utils.settle_decided_draft("s1")

    draw.assert_awaited_once()


@pytest.mark.asyncio
async def test_releasing_and_deleting_a_queue_is_one_step():
    """Release and delete as two transactions leave a window in which an entry
    can be charged into a row that is about to go. As one, under MONEY_LOCK,
    no charge can land between them -- and a failed release deletes nothing."""
    with patch.object(pool, "_refund_in", failing_refund()):
        with pytest.raises(pool.PoolNotSettled):
            await pool.release_draft_pool("g", "s1", "cancelled", delete_draft=True)
    assert await get_draft_session("s1") is not None

    await pool.release_draft_pool("g", "s1", "cancelled", delete_draft=True)

    assert await get_draft_session("s1") is None
    assert await pool.pool_balance("g", "s1") == 0


@pytest.mark.asyncio
async def test_nothing_can_be_charged_into_a_cancelled_queue():
    """A stake selector opened before the cancel stays live for minutes."""
    await _with_a_channel()
    views, view, interaction, _ = _cancel_view()

    with patch.object(views.DraftSetupManager, "cancel_for_session", AsyncMock()):
        await view.confirm_button.callback(interaction)
    result = await pool.set_entry("g", "s1", "p1", 30)

    assert not result["ok"]
    assert await pool.pool_balance("g", "s1") == 0


@pytest.mark.asyncio
async def test_a_failed_announcement_does_not_skip_the_rest_of_the_cancel():
    """Each cleanup step stands alone: the announcement failing must not leave
    the draft's manager running under a draft that is about to be deleted."""
    await _with_a_channel()
    views, view, interaction, channel = _cancel_view()
    channel.send = AsyncMock(side_effect=RuntimeError("Discord 503"))
    sign_up = MagicMock(delete=AsyncMock())
    channel.fetch_message = AsyncMock(return_value=sign_up)

    with patch.object(views.DraftSetupManager, "cancel_for_session", AsyncMock()) as stop:
        await view.confirm_button.callback(interaction)

    stop.assert_awaited_once_with("s1")
    sign_up.delete.assert_awaited_once()
