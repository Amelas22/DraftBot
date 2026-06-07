"""TDD test for the team_creator.py UnboundLocalError bug.

When a premade (or any non-random/test/staked/winston) draft reaches
create_and_display_teams, the function crashes at line 129/133 because
`stake_info_by_player` is only initialized inside the conditional block at
line 71. The function's broad `except Exception` swallows it and reports
"An error occurred while creating teams" — leaving the draft stuck.

Fired ≥16 times in prod across guild 1399789864961970337 and guild
336345350535118849 — both are non-money servers that run premade drafts.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_create_and_display_teams_does_not_raise_unbound_local_for_premade():
    """For a premade draft, the function must complete without hitting
    UnboundLocalError on `stake_info_by_player`."""
    from services import team_creator

    # 6-player premade with teams pre-assigned (matches the prod scenario)
    sign_ups = {
        "111": "PlayerA1", "222": "PlayerA2", "333": "PlayerA3",
        "444": "PlayerB1", "555": "PlayerB2", "666": "PlayerB3",
    }
    session = MagicMock()
    session.session_type = "premade"
    session.session_id = "fake-session-123"
    session.sign_ups = sign_ups
    session.team_a = ["111", "222", "333"]
    session.team_b = ["444", "555", "666"]
    session.tracked_draft = False
    session.premade_match_id = None

    # Mock the DB plumbing: AsyncSessionLocal() → db_session, db_session.begin() → tx
    select_result = MagicMock()
    select_result.scalars.return_value.first.return_value = session

    db_session_inner = MagicMock()
    db_session_inner.execute = AsyncMock(return_value=select_result)
    db_session_inner.commit = AsyncMock()
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=db_session_inner)
    begin_ctx.__aexit__ = AsyncMock(return_value=None)
    db_session_inner.begin = MagicMock(return_value=begin_ctx)

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=db_session_inner)
    session_ctx.__aexit__ = AsyncMock(return_value=None)

    interaction = MagicMock()
    interaction.guild_id = 336345350535118849
    interaction.guild.name = "TestGuild"
    interaction.followup.send = AsyncMock()
    interaction.followup.edit_message = AsyncMock()
    interaction.message.id = "msg123"
    interaction.channel.id = "chan123"
    interaction.channel.name = "draft-channel"
    interaction.channel.send = AsyncMock()
    interaction.client = MagicMock()

    persistent_view = MagicMock()
    persistent_view.session_type = "premade"
    persistent_view.children = []

    bot = MagicMock()
    seating_order = list(sign_ups.values())

    with patch.object(team_creator, "AsyncSessionLocal", MagicMock(return_value=session_ctx)), \
         patch.object(team_creator, "generate_seating_order", AsyncMock(return_value=seating_order)), \
         patch.object(team_creator, "_create_teams_embed", AsyncMock(return_value=MagicMock())), \
         patch.object(team_creator, "_create_channel_announcement_embed", AsyncMock(return_value=MagicMock())), \
         patch.object(team_creator, "_update_draft_manager", AsyncMock()), \
         patch.object(team_creator, "send_teams_created_dms", AsyncMock()), \
         patch.object(team_creator, "state_manager"), \
         patch.object(team_creator, "logger") as mock_logger:
        result = await team_creator.create_and_display_teams(
            bot, "fake-session-123", interaction, persistent_view,
        )

    # The function's broad `except Exception` swallows the UnboundLocalError
    # and routes it to logger.exception. Assert no such call happened.
    logged_errors = [str(call) for call in mock_logger.exception.call_args_list]
    assert not any("stake_info_by_player" in msg for msg in logged_errors), (
        "create_and_display_teams hit UnboundLocalError on stake_info_by_player "
        f"for premade drafts: {logged_errors}"
    )
    assert result is True, (
        "create_and_display_teams returned False, meaning the broad except caught "
        "an exception — likely the stake_info_by_player UnboundLocalError"
    )
