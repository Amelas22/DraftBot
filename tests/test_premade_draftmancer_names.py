"""Regression test: premade team-join must store the RAW nickname in sign_ups.

sign_ups values are used verbatim as Draftmancer usernames (get_draft_link_for_user)
and for seating matches. get_display_name() prepends custom-emoji icons like
"<:coveted_jewel:1460802711694999613>", which render as that literal code in
Draftmancer. Every other sign-up path stores interaction.user.display_name (raw);
the premade path must too.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from views import PersistentView


def _async_session_local_mock():
    """Mock for `async with AsyncSessionLocal() as db: async with db.begin(): ...`."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    begin = MagicMock()
    begin.__aenter__ = AsyncMock(return_value=None)
    begin.__aexit__ = AsyncMock(return_value=False)
    db.begin = MagicMock(return_value=begin)
    outer = MagicMock()
    outer.__aenter__ = AsyncMock(return_value=db)
    outer.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=outer)


@pytest.mark.asyncio
async def test_premade_join_stores_plain_name_not_emoji_decorated():
    # Non-empty sign_ups mirrors reality (others have already joined) and avoids
    # the `session.sign_ups or {}` empty-dict shortcut making a fresh dict.
    session = SimpleNamespace(
        session_id="s1", sign_ups={"99": "Bob"},
        team_a=[], team_b=[], team_a_name="Team A", team_b_name="Team B",
    )
    view = PersistentView(
        bot=MagicMock(), draft_session_id="s1", session_type="premade",
        team_a_name="Team A", team_b_name="Team B",
    )
    view.update_team_view = AsyncMock()

    interaction = MagicMock()
    interaction.user.id = 1
    interaction.user.display_name = "Alice"
    interaction.guild = MagicMock()
    interaction.response.send_message = AsyncMock()
    button = MagicMock()
    button.custom_id = "Team_A_s1"

    # If the code used get_display_name, this decorated value would leak into
    # sign_ups (and thus the Draftmancer link). It must not.
    decorated = "<:coveted_jewel:1460802711694999613> Alice"
    with patch("views.get_draft_session", AsyncMock(return_value=session)), \
         patch("views.AsyncSessionLocal", _async_session_local_mock()), \
         patch("views.get_display_name", lambda member, guild=None: decorated):
        await view.team_assignment_callback(interaction, button)

    assert session.sign_ups["1"] == "Alice"
