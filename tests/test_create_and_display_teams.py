"""Tests for premade create_and_display_teams.

Covers two prod regressions:
1. UnboundLocalError on `stake_info_by_player` (initialized only inside a
   conditional block) — premade drafts got stuck.
2. KeyError in the seating name->id reverse lookup: generate_seating_order
   returns DECORATED/escaped display names (role icons, markdown-escaped), which
   after PR #332 no longer match the RAW names stored in sign_ups. The mapping
   must use the user_id, not a name reverse-lookup (seen in prod today on
   '👑 AlicanGokturk' and 'The\\_Tank').
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _premade_session(sign_ups, team_a=None, team_b=None):
    session = MagicMock()
    session.session_type = "premade"
    session.session_id = "fake-session-123"
    session.sign_ups = dict(sign_ups)
    ids = list(sign_ups.keys())
    session.team_a = team_a if team_a is not None else ids[:len(ids) // 2]
    session.team_b = team_b if team_b is not None else ids[len(ids) // 2:]
    session.tracked_draft = False
    session.premade_match_id = None
    return session


async def _run_premade(sign_ups, seating_return, team_a=None, team_b=None):
    """Run create_and_display_teams for a premade draft with generate_seating_order
    mocked to return `seating_return`. Returns (result, mock_logger, session)."""
    from services import team_creator

    session = _premade_session(sign_ups, team_a=team_a, team_b=team_b)
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

    with patch.object(team_creator, "AsyncSessionLocal", MagicMock(return_value=session_ctx)), \
         patch.object(team_creator, "generate_seating_order", AsyncMock(return_value=seating_return)), \
         patch.object(team_creator, "_create_teams_embed", AsyncMock(return_value=MagicMock())), \
         patch.object(team_creator, "_create_channel_announcement_embed", AsyncMock(return_value=MagicMock())), \
         patch.object(team_creator, "_update_draft_manager", AsyncMock()), \
         patch.object(team_creator, "send_teams_created_dms", AsyncMock()), \
         patch.object(team_creator, "state_manager"), \
         patch.object(team_creator, "logger") as mock_logger:
        result = await team_creator.create_and_display_teams(
            bot, "fake-session-123", interaction, persistent_view,
        )
    return result, mock_logger, session


@pytest.mark.asyncio
async def test_create_and_display_teams_does_not_raise_unbound_local_for_premade():
    """For a premade draft, the function must complete without hitting
    UnboundLocalError on `stake_info_by_player`."""
    sign_ups = {
        "111": "PlayerA1", "222": "PlayerA2", "333": "PlayerA3",
        "444": "PlayerB1", "555": "PlayerB2", "666": "PlayerB3",
    }
    seating = [(uid, nm) for uid, nm in sign_ups.items()]
    result, mock_logger, _ = await _run_premade(sign_ups, seating)

    logged = [str(c) for c in mock_logger.exception.call_args_list]
    assert not any("stake_info_by_player" in m for m in logged), logged
    assert result is True


@pytest.mark.asyncio
async def test_premade_seating_maps_by_id_not_decorated_name():
    """generate_seating_order returns (user_id, decorated_name) pairs whose names
    differ from the RAW sign_ups values (crown icon / markdown-escaped underscore).
    Team creation must map by user_id and not KeyError, and must keep the RAW names
    in sign_ups (preserving the PR #332 invariant), reordered to the seating."""
    sign_ups = {  # RAW names, as stored post-#332
        "111": "AlicanGokturk", "222": "The_Tank", "333": "PlayerA3",
        "444": "PlayerB1", "555": "PlayerB2", "666": "PlayerB3",
    }
    seating = [  # (id, DECORATED name) — names NOT present as sign_ups values
        ("111", "👑 AlicanGokturk"), ("444", "PlayerB1"),
        ("222", "The\\_Tank"), ("555", "PlayerB2"),
        ("333", "PlayerA3"), ("666", "PlayerB3"),
    ]
    result, mock_logger, session = await _run_premade(sign_ups, seating)

    logged = [str(c) for c in mock_logger.exception.call_args_list]
    assert not any("KeyError" in m or "AlicanGokturk" in m for m in logged), (
        f"premade seating crashed on a decorated name: {logged}")
    assert result is True
    # sign_ups reordered by seating, keyed by id, values still the RAW names
    assert set(session.sign_ups.keys()) == set(sign_ups)
    assert all(session.sign_ups[uid] == sign_ups[uid] for uid in sign_ups)
    assert list(session.sign_ups.keys()) == [uid for uid, _ in seating]


@pytest.mark.asyncio
async def test_premade_rejects_unbalanced_teams():
    """A premade with an even total but a lopsided split (2-vs-0) must be rejected,
    not seated into a broken draft with only one team (Codex finding)."""
    sign_ups = {"111": "A", "222": "B"}                 # even total = 2
    result, _mock_logger, _session = await _run_premade(
        sign_ups, [("111", "A"), ("222", "B")], team_a=["111", "222"], team_b=[])
    assert result is False


@pytest.mark.asyncio
async def test_premade_balance_counts_only_signed_up_members():
    """Balance is measured on team members still in sign_ups — a stale id left in a
    team (from the leave flow) must not tip an otherwise-balanced draft into rejection."""
    sign_ups = {"111": "A", "222": "B", "333": "C", "444": "D"}
    result, _mock_logger, _session = await _run_premade(
        sign_ups, [("111", "A"), ("333", "C"), ("222", "B"), ("444", "D")],
        team_a=["111", "222", "999"], team_b=["333", "444"])   # 999 not signed up
    assert result is True                                # effective 2-vs-2 → allowed
