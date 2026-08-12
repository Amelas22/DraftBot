"""#397: plain-text channel announcements must say which draft they're about.

Signup channels carry several concurrent drafts, so "User X has cancelled the
draft" leaves readers unable to tell which queue just died. Embeds solve this
with the metadata footer (helpers/draft_footer.py); these messages have no
footer to hang it on and carry an inline `friendly_id` instead.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import views


def _session(friendly_id="lightning-bolt-7"):
    return SimpleNamespace(
        session_id="123456789012345678-1753500000",
        friendly_id=friendly_id,
        cube="LSVCube",
        draft_channel_id="111",
        message_id="222",
    )


async def _run_cancel(session):
    """Drive CancelConfirmationView.confirm_button and return what it announced.

    Everything past the announcement (Draftmancer teardown, message deletion,
    the DB delete) is stubbed — this test is only about the wording.
    """
    channel = MagicMock()
    channel.send = AsyncMock()
    channel.fetch_message = AsyncMock(side_effect=Exception("no message in test"))

    bot = MagicMock()
    bot.get_channel.return_value = channel

    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup.send = AsyncMock()

    view = views.CancelConfirmationView(bot, session.session_id, "ilaggoodly")

    manager_cls = MagicMock()
    manager_cls.get_active_manager.return_value = None

    db_session = MagicMock()
    db_session.delete = AsyncMock()
    db_session.commit = AsyncMock()
    db_session.begin.return_value.__aenter__ = AsyncMock(return_value=db_session)
    db_session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    session_local = MagicMock()
    session_local.return_value.__aenter__ = AsyncMock(return_value=db_session)
    session_local.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch.object(views, "get_draft_session", AsyncMock(return_value=session)), \
         patch.object(views, "AsyncSessionLocal", session_local), \
         patch.object(views.ReadyCheckSession, "cleanup", AsyncMock()), \
         patch.dict("sys.modules", {
             "services.draft_setup_manager": SimpleNamespace(
                 DraftSetupManager=manager_cls, ACTIVE_MANAGERS={}
             )
         }):
        await view.confirm_button.callback(interaction)

    channel.send.assert_awaited_once()
    return channel.send.await_args.args[0]


@pytest.mark.asyncio
async def test_cancel_announcement_names_the_draft():
    announcement = await _run_cancel(_session())

    # "the draft" stays in even when the id is present: a bare code span after
    # the verb doesn't tell the reader what kind of thing was cancelled.
    assert announcement == "User **ilaggoodly** has cancelled the draft `lightning-bolt-7`."


@pytest.mark.asyncio
async def test_cancel_announcement_falls_back_when_the_row_has_no_friendly_id():
    # Rows predating friendly_id must still produce a sentence, not "`None`".
    announcement = await _run_cancel(_session(friendly_id=None))

    assert announcement == "User **ilaggoodly** has cancelled the draft."
    assert "None" not in announcement


@pytest.mark.parametrize("friendly_id, expected", [
    ("lightning-bolt-7", "8 Players in queue for draft `lightning-bolt-7`! @drafter"),
    (None, "8 Players in queue! @drafter"),
])
def test_queue_ping_names_the_draft(friendly_id, expected):
    # The @drafter ping lands in a channel hosting several queues; unqualified,
    # it doesn't say which one is filling up.
    assert views.queue_ping_text(8, _session(friendly_id), "@drafter") == expected
