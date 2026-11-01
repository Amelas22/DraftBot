"""A legacy (manager) draft stays on the public draftmancer.com.

Once hosting is on, DRAFTMANCER_BASE_URL names our own server. A draft run by a
DraftSetupManager -- one in flight at the cutover, one reconnect_drafts or the
log reconciler respawns, or any draft while hosting is off -- was opened on
draftmancer.com, and its socket, its links and its notices must stay there.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from models.draft_session import DraftSession

from conftest import make_manager

OUR_SERVER = "https://draft.draftbot.example"
PUBLIC = "https://draftmancer.com"

pytestmark = pytest.mark.usefixtures("clean_manager_registry")


@pytest.fixture
def our_server(monkeypatch):
    monkeypatch.setenv("DRAFTMANCER_BASE_URL", OUR_SERVER)
    monkeypatch.delenv("PUBLIC_DRAFTMANCER_URL", raising=False)


def test_the_managers_socket_goes_to_draftmancer_com(our_server):
    from config import get_draftmancer_websocket_url

    assert get_draftmancer_websocket_url("LEG1").startswith("wss://draftmancer.com?")


@pytest.mark.asyncio
async def test_a_manager_connects_to_draftmancer_com(our_server):
    manager = make_manager(draft_id="LEG1")
    manager.socket_client.connect_with_retry = AsyncMock(return_value=False)

    await manager.connect_to_new_session()

    url = manager.socket_client.connect_with_retry.await_args.args[0]
    assert url.startswith("wss://draftmancer.com?") and "sessionID=DBLEG1" in url


@pytest.mark.asyncio
async def test_a_manager_queue_hands_out_draftmancer_com_links(test_db, our_server, monkeypatch):
    """Hosting off: the queue is a manager draft, and its links are draftmancer.com's."""
    from models.session_details import SessionDetails
    from sessions.random_session import RandomSession

    monkeypatch.delenv("DRAFTMANCER_API_URL", raising=False)
    monkeypatch.delenv("DRAFTMANCER_API_TOKEN", raising=False)
    interaction = MagicMock(spec_set=discord.Interaction)
    interaction.user = MagicMock(id=123456, display_name="Host")
    interaction.guild_id = 789012
    interaction.guild = MagicMock(id=789012)
    interaction.response = AsyncMock()
    message = MagicMock(id="987654321")
    message.channel = MagicMock(id="111222333")
    interaction.original_response = AsyncMock(return_value=message)
    interaction.client = MagicMock()
    details = SessionDetails(interaction)
    details.cube_choice = "LSVCube"

    with patch("sessions.base_session.DraftSetupManager") as manager, \
         patch("sessions.base_session.PersistentView"), \
         patch("sessions.base_session.make_message_sticky", new_callable=AsyncMock), \
         patch("sessions.base_session.get_session_deletion_hours", return_value=5), \
         patch("sessions.base_session.get_cube_thumbnail_url", return_value="https://x/t.jpg"):
        manager.return_value.keep_connection_alive = AsyncMock()
        session = RandomSession(details, session_factory=AsyncSessionLocal)
        await session.create_draft_session(interaction, interaction.client)

    manager.assert_called_once()
    async with AsyncSessionLocal() as s:
        row = await s.scalar(select(DraftSession).filter_by(session_id=details.session_id))
    assert row is not None and row.draftmancer_route is None
    assert row.draft_link == f"{PUBLIC}/?session=DB{details.draft_id}"
    assert row.get_draft_link_for_user("Ann", user_id="1").startswith(f"{PUBLIC}/?session=")


def test_the_managers_session_links_are_draftmancer_coms(our_server):
    """Its disconnect and stand-down notices, and a regenerated room's link."""
    import inspect

    from services import draft_setup_manager

    src = inspect.getsource(draft_setup_manager)
    assert "get_draftmancer_session_url" not in src
    assert draft_setup_manager.public_draftmancer_session_url("X1") == f"{PUBLIC}/?session=DBX1"
