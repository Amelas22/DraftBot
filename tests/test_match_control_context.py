"""Match-thread lookup and control-message facts, against a real SQLite session."""
import pytest

from conftest import match_control_db, seed_tournament_match  # noqa: F401  (fixture)
from models.draft_session import DraftSession
from services.tournament_service import set_result


async def _link_draft(session, match_id, session_id="d1"):
    session.add(DraftSession(
        session_id=session_id, guild_id="g1", session_type="premade",
        draft_channel_id="55", message_id="66", tournament_match_id=match_id,
    ))
    await session.commit()


@pytest.mark.asyncio
async def test_match_facts_returns_team_names_and_round(match_control_db):
    from match_control_view import match_facts

    async with match_control_db() as session:
        match = await seed_tournament_match(session, thread_id="900")
        _m, a_name, b_name, round_number, draft = await match_facts(session, match.id)

    assert {a_name, b_name} == {"Alpha", "Bravo"}
    assert round_number == 1
    assert draft is None


@pytest.mark.asyncio
async def test_match_facts_finds_the_linked_draft(match_control_db):
    from match_control_view import match_facts

    async with match_control_db() as session:
        match = await seed_tournament_match(session, thread_id="900")
        await _link_draft(session, match.id)
        _m, _a, _b, _r, draft = await match_facts(session, match.id)

    assert draft is not None and draft.session_id == "d1"


@pytest.mark.asyncio
async def test_match_facts_is_none_for_a_missing_match(match_control_db):
    from match_control_view import match_facts

    async with match_control_db() as session:
        assert await match_facts(session, 999999) is None


def test_lobby_link_is_none_without_a_draft():
    from match_control_view import lobby_link

    assert lobby_link(None) is None


def test_lobby_link_points_at_the_lobby_message():
    from match_control_view import lobby_link

    draft = DraftSession(session_id="d1", guild_id="7", draft_channel_id="8", message_id="9")
    assert lobby_link(draft) == "https://discord.com/channels/7/8/9"


@pytest.mark.asyncio
async def test_control_body_offers_start_when_scheduling(match_control_db):
    from match_control_view import control_body_and_view

    async with match_control_db() as session:
        match = await seed_tournament_match(session, thread_id="900")
        body, view = control_body_and_view(match, "Alpha", "Bravo", 1, None)

    assert "Not started yet" in body
    assert view is not None


@pytest.mark.asyncio
async def test_control_body_drops_the_button_once_drafting(match_control_db):
    from match_control_view import control_body_and_view
    from models.draft_session import DraftSession as DS

    async with match_control_db() as session:
        match = await seed_tournament_match(session, thread_id="900")
        draft = DS(session_id="d1", guild_id="7", draft_channel_id="8", message_id="9")
        body, view = control_body_and_view(match, "Alpha", "Bravo", 1, draft)

    assert "Draft in progress" in body
    assert "https://discord.com/channels/7/8/9" in body
    assert view is None


@pytest.mark.asyncio
async def test_context_is_none_outside_a_match_thread(match_control_db):
    from match_control_view import match_room_context

    async with match_control_db() as session:
        await seed_tournament_match(session, thread_id="900")
        assert await match_room_context(session, 12345) is None


@pytest.mark.asyncio
async def test_context_supplies_both_team_names_as_overrides(match_control_db):
    from match_control_view import match_room_context

    async with match_control_db() as session:
        match = await seed_tournament_match(session, thread_id="900")
        match_id, overrides, block = await match_room_context(session, 900)

    assert match_id == match.id
    assert overrides["tournament_match_id"] == match.id
    # Both names present is what makes CubeDraftModal drop its name inputs.
    assert set(overrides) == {"tournament_match_id", "team_a_name", "team_b_name"}
    assert block is None


@pytest.mark.asyncio
async def test_context_blocks_when_a_draft_is_already_live(match_control_db):
    from match_control_view import match_room_context

    async with match_control_db() as session:
        match = await seed_tournament_match(session, thread_id="900")
        await _link_draft(session, match.id)
        _id, _overrides, block = await match_room_context(session, 900)

    assert "already underway" in block
    assert "https://discord.com/channels/g1/55/66" in block


@pytest.mark.asyncio
async def test_context_blocks_when_the_match_is_already_recorded(match_control_db):
    from match_control_view import match_room_context

    async with match_control_db() as session:
        match = await seed_tournament_match(session, thread_id="900")
        await set_result(session, match.id, 2, 1)
        await session.commit()
        _id, _overrides, block = await match_room_context(session, 900)

    assert "Result recorded" in block
