from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers.substitutes import (
    GrantDecision,
    channel_ids_contains,
    is_sub_target_channel,
    resolve_sub_grant,
)
from models.draft_session import DraftSession


def make_session(team_a=None, team_b=None, sign_ups=None,
                 team_a_name=None, team_b_name=None):
    return SimpleNamespace(
        team_a=team_a or [],
        team_b=team_b or [],
        sign_ups=sign_ups or {},
        team_a_name=team_a_name,
        team_b_name=team_b_name,
    )


# ---- resolve_sub_grant ------------------------------------------------------

def test_player_on_team_a_grants_red_team():
    session = make_session(team_a=["1"], team_b=["2"])
    decision, error = resolve_sub_grant(session, invoker_id="1", target_id="9",
                                        is_admin=False)
    assert error is None
    assert decision.team_key == "A"
    assert decision.channel_prefix == "Red-Team"
    assert decision.team_display_name == "Red Team"  # fallback when name unset


def test_player_on_team_b_grants_blue_team():
    session = make_session(team_a=["1"], team_b=["2"])
    decision, error = resolve_sub_grant(session, invoker_id="2", target_id="9",
                                        is_admin=False)
    assert error is None
    assert decision.team_key == "B"
    assert decision.channel_prefix == "Blue-Team"


def test_premade_team_name_used_for_display():
    session = make_session(team_a=["1"], team_b=["2"],
                           team_a_name="Goblin Gang", team_b_name="Merfolk Mob")
    decision, _ = resolve_sub_grant(session, invoker_id="1", target_id="9",
                                    is_admin=False)
    assert decision.team_display_name == "Goblin Gang"


def test_player_team_choice_is_ignored():
    """A player always grants their own team, even if they pass team_choice."""
    session = make_session(team_a=["1"], team_b=["2"])
    decision, error = resolve_sub_grant(session, invoker_id="1", target_id="9",
                                        is_admin=False, team_choice="B")
    assert error is None
    assert decision.team_key == "A"


def test_admin_not_in_draft_uses_team_choice():
    session = make_session(team_a=["1"], team_b=["2"])
    decision, error = resolve_sub_grant(session, invoker_id="99", target_id="9",
                                        is_admin=True, team_choice="B")
    assert error is None
    assert decision.team_key == "B"
    assert decision.channel_prefix == "Blue-Team"


def test_admin_without_team_choice_is_error():
    session = make_session(team_a=["1"], team_b=["2"])
    decision, error = resolve_sub_grant(session, invoker_id="99", target_id="9",
                                        is_admin=True)
    assert decision is None
    assert "team" in error.lower()


def test_non_participant_non_admin_is_error():
    session = make_session(team_a=["1"], team_b=["2"])
    decision, error = resolve_sub_grant(session, invoker_id="99", target_id="9",
                                        is_admin=False)
    assert decision is None
    assert error is not None


def test_target_already_on_a_team_is_error():
    session = make_session(team_a=["1"], team_b=["2"])
    decision, error = resolve_sub_grant(session, invoker_id="1", target_id="2",
                                        is_admin=False)
    assert decision is None
    assert "already" in error.lower()


def test_target_in_sign_ups_is_error():
    session = make_session(team_a=["1"], team_b=["2"], sign_ups={"9": "Niner"})
    decision, error = resolve_sub_grant(session, invoker_id="1", target_id="9",
                                        is_admin=False)
    assert decision is None
    assert "already" in error.lower()


def test_none_team_fields_are_tolerated():
    """None team_a/team_b/sign_ups is a team-less draft — no crash, draft chat only."""
    session = SimpleNamespace(team_a=None, team_b=None, sign_ups=None,
                              team_a_name=None, team_b_name=None)
    decision, error = resolve_sub_grant(session, invoker_id="99", target_id="9",
                                        is_admin=True, team_choice="A")
    assert error is None
    assert decision.team_key is None
    assert decision.channel_prefix is None


# ---- team-less drafts (e.g. swiss) ------------------------------------------

def test_teamless_draft_participant_gets_draft_chat_only():
    session = make_session(team_a=[], team_b=[], sign_ups={"p1": "P1"})
    decision, error = resolve_sub_grant(session, invoker_id="p1", target_id="sub",
                                        is_admin=False)
    assert error is None
    assert decision.channel_prefix is None
    assert decision.team_key is None
    assert decision.team_display_name == "this draft"


def test_teamless_draft_admin_gets_draft_chat_only():
    session = make_session(team_a=[], team_b=[], sign_ups={"p1": "P1"})
    decision, error = resolve_sub_grant(session, invoker_id="admin", target_id="sub",
                                        is_admin=True)
    assert error is None
    assert decision.channel_prefix is None
    assert decision.team_key is None


def test_teamless_draft_non_participant_non_admin_rejected():
    session = make_session(team_a=[], team_b=[], sign_ups={"p1": "P1"})
    decision, error = resolve_sub_grant(session, invoker_id="stranger", target_id="sub",
                                        is_admin=False)
    assert decision is None
    assert error is not None


def test_teamless_target_channel_matches_draft_chat_only():
    assert is_sub_target_channel("draft-chat-ABC", "ABC", None) is True
    assert is_sub_target_channel("red-team-chat-ABC", "ABC", None) is False


# ---- is_sub_target_channel --------------------------------------------------

def test_matches_lowercased_discord_text_channel_names():
    # Discord lowercases text channel names on creation
    assert is_sub_target_channel("draft-chat-AbC123".lower(), "AbC123", "Red-Team")
    assert is_sub_target_channel("red-team-chat-abc123", "AbC123", "Red-Team")


def test_matches_voice_channel_with_original_case():
    assert is_sub_target_channel("Red-Team-Voice-AbC123", "AbC123", "Red-Team")


def test_rejects_other_teams_channels():
    assert not is_sub_target_channel("blue-team-chat-abc123", "AbC123", "Red-Team")
    assert not is_sub_target_channel("Blue-Team-Voice-AbC123", "AbC123", "Red-Team")


def test_rejects_channels_of_other_drafts():
    assert not is_sub_target_channel("red-team-chat-zzz999", "AbC123", "Red-Team")


# ---- channel_ids_contains ---------------------------------------------------

def test_contains_handles_int_stored_ids_and_str_query():
    assert channel_ids_contains([111, 222], "222")
    assert channel_ids_contains([111, 222], 111)


def test_contains_handles_str_stored_ids():
    assert channel_ids_contains(["111", "222"], 222)


def test_contains_handles_none_and_empty():
    assert not channel_ids_contains(None, 111)
    assert not channel_ids_contains([], 111)


# ---- DraftSession.get_by_any_channel_id --------------------------------------

def _mock_db_session(drafts):
    """Async-context-manager mock for models.draft_session.db_session whose
    execute() returns the given drafts via .scalars().all()."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = drafts
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    db = MagicMock(return_value=ctx)
    return db


@pytest.mark.asyncio
async def test_get_by_any_channel_id_prefers_draft_chat_match():
    direct_hit = SimpleNamespace(session_id="s1")
    with patch.object(DraftSession, "get_by_channel_id",
                      AsyncMock(return_value=direct_hit)) as direct:
        found = await DraftSession.get_by_any_channel_id(555)
    assert found is direct_hit
    direct.assert_awaited_once_with("555")


@pytest.mark.asyncio
async def test_get_by_any_channel_id_falls_back_to_channel_ids():
    other = SimpleNamespace(session_id="s0", channel_ids=[333])
    match = SimpleNamespace(session_id="s1", channel_ids=[111, 222])  # ints, as stored
    with patch.object(DraftSession, "get_by_channel_id",
                      AsyncMock(return_value=None)), \
         patch("models.draft_session.db_session", _mock_db_session([other, match])):
        found = await DraftSession.get_by_any_channel_id("222")
    assert found is match


@pytest.mark.asyncio
async def test_get_by_any_channel_id_returns_none_when_no_match():
    with patch.object(DraftSession, "get_by_channel_id",
                      AsyncMock(return_value=None)), \
         patch("models.draft_session.db_session", _mock_db_session([])):
        found = await DraftSession.get_by_any_channel_id(999)
    assert found is None
