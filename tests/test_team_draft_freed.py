"""A team draft whose draft is deleted before it ran gets its matchup back.

Cancel and the unfilled-queue reaper both delete the draft's row through
release_draft_pool(delete_draft=True). The matchup must not keep pointing at
the dead draft: Start draft would then refuse with "already been played"
for a draft that never happened.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import run_until_sleep, seed_queue
from database.db_session import db_session
from models.team_draft import TeamDraft
from test_team_draft_room import make_guild, make_interaction, seed_team_draft, stored


@pytest.fixture(autouse=True)
def scheduler_on():
    with patch("team_draft_view.scheduler_enabled", return_value=True):
        yield


@pytest.fixture
def bot():
    import bot_registry
    bot = MagicMock()
    bot.get_channel.return_value = None
    bot_registry.register_bot(bot)
    yield bot
    bot_registry.register_bot(None)


async def claimed_matchup(session_id):
    team_draft_id = await seed_team_draft(thread_id="900", control_message_id="777")
    async with db_session() as session:
        (await session.get(TeamDraft, team_draft_id)).draft_session_id = session_id
    return team_draft_id


async def assert_start_draft_offers_the_picker(team_draft_id):
    from team_draft_view import start_team_draft

    interaction = make_interaction(make_guild())
    with patch("modals.CubeDraftSelectionView") as picker:
        await start_team_draft(interaction, team_draft_id)
    picker.assert_called_once()
    assert interaction.response.send_message.await_args.kwargs["view"] is picker.return_value


@pytest.mark.asyncio
async def test_release_deleted_draft_frees_only_the_matchup_holding_it(test_db):
    from services.team_draft import release_deleted_draft

    held = await claimed_matchup("s-dead")
    other = await claimed_matchup("s-live")
    async with db_session() as session:
        assert await release_deleted_draft(session, "s-dead") == held
    async with db_session() as session:
        assert await release_deleted_draft(session, "s-nobody") is None

    assert (await stored(held)).draft_session_id is None
    assert (await stored(other)).draft_session_id == "s-live"


@pytest.mark.asyncio
async def test_a_cancelled_draft_frees_its_matchup(test_db, bot):
    import views

    await seed_queue("s1", draft_channel_id="123")
    team_draft_id = await claimed_matchup("s1")
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    view = views.CancelConfirmationView(bot, "s1", "Ann")

    with patch.object(views.DraftSetupManager, "cancel_for_session", AsyncMock()), \
         patch("team_draft_view.refresh_team_draft_control", AsyncMock()) as refresh:
        await view.confirm_button.callback(interaction)

    assert (await stored(team_draft_id)).draft_session_id is None
    refresh.assert_awaited_once_with(bot, team_draft_id)
    await assert_start_draft_offers_the_picker(team_draft_id)


@pytest.mark.asyncio
async def test_an_expired_queue_frees_its_matchup(test_db, bot):
    from utils import cleanup_sessions_task

    await seed_queue("idle", deletion_time=datetime.now() - timedelta(minutes=1))
    team_draft_id = await claimed_matchup("idle")

    with patch("team_draft_view.refresh_team_draft_control", AsyncMock()) as refresh:
        await run_until_sleep(cleanup_sessions_task(bot), 600)

    assert (await stored(team_draft_id)).draft_session_id is None
    refresh.assert_awaited_once_with(bot, team_draft_id)
    await assert_start_draft_offers_the_picker(team_draft_id)


@pytest.mark.asyncio
async def test_a_failing_refresh_never_breaks_the_cancel(test_db, bot):
    from services import draft_pool_service as pool

    await seed_queue("s1")
    team_draft_id = await claimed_matchup("s1")

    with patch("team_draft_view.refresh_team_draft_control",
               AsyncMock(side_effect=RuntimeError("discord down"))):
        await pool.release_draft_pool("g", "s1", "cancelled", delete_draft=True)

    assert (await stored(team_draft_id)).draft_session_id is None


@pytest.mark.asyncio
async def test_a_release_without_delete_keeps_the_matchup(test_db, bot):
    from services import draft_pool_service as pool

    await seed_queue("s1")
    team_draft_id = await claimed_matchup("s1")

    with patch("team_draft_view.refresh_team_draft_control", AsyncMock()) as refresh:
        await pool.release_draft_pool("g", "s1", "abandoned")

    assert (await stored(team_draft_id)).draft_session_id == "s1"
    refresh.assert_not_awaited()
