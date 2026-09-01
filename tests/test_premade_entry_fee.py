"""A premade draft can charge a fixed entry fee, set when it is created.

Unlike a staked queue, nobody chooses an amount: everyone on either side pays
the same. That makes the two sides level by construction as long as the teams
are the same size, so the pool is always exactly twice what one side put in and
no player is ever partially refunded to make the arithmetic work.
"""
import pytest
import pytest_asyncio
from sqlalchemy import select, update

from conftest import seed_session
from database.db_session import db_session
from models.draft_session import DraftSession
from services import draft_pool_service as pool
from services import wallet_service


@pytest_asyncio.fixture(autouse=True)
async def _premade(test_db):
    await seed_session("pm", guild="g", stype="premade", stage=None,
                       teams=([], []), sign_ups={})
    async with db_session() as s:
        await s.execute(update(DraftSession)
                        .where(DraftSession.session_id == "pm")
                        .values(entry_fee=50))


async def _join(player, team):
    """What team_assignment_callback does: charge the fee and join the side, in
    one transaction."""
    async with wallet_service.MONEY_LOCK:
        async with db_session() as s:
            draft = (await s.execute(select(DraftSession)
                     .where(DraftSession.session_id == "pm"))).scalars().first()
            charged = await pool.entry_in(s, "g", "pm", player,
                                          draft.entry_fee or 0, "joined")
            if not charged["ok"]:
                raise AssertionError(f"could not join: {charged}")
            side = list(getattr(draft, team) or [])
            side.append(player)
            setattr(draft, team, side)
            draft.sign_ups = {**(draft.sign_ups or {}), player: player}
    return charged


@pytest.mark.asyncio
async def test_everyone_pays_the_same_fee_and_the_sides_come_out_level(test_db):
    for p in ("a1", "a2", "b1", "b2"):
        await wallet_service.adjust("g", p, 200, "seed", "t")
    for p in ("a1", "a2"):
        await _join(p, "team_a")
    for p in ("b1", "b2"):
        await _join(p, "team_b")

    held = await pool.contributions("g", "pm")
    assert held == {"a1": 50, "a2": 50, "b1": 50, "b2": 50}
    assert await pool.pool_balance("g", "pm") == 200

    # Levelling has nothing to do: equal teams paying a fixed fee are already
    # level, so nobody is refunded and everyone keeps their whole entry.
    result = await pool.match_pool("g", "pm", ["a1", "a2"], ["b1", "b2"])

    assert result["refunded"] == {}, f"a fixed fee was partially refunded: {result}"
    assert result["matched"] == 100
    assert await pool.contributions("g", "pm") == held


@pytest.mark.asyncio
async def test_the_winning_side_takes_double_the_fee(test_db):
    for p in ("a1", "a2", "b1", "b2"):
        await wallet_service.adjust("g", p, 200, "seed", "t")
    for p in ("a1", "a2"):
        await _join(p, "team_a")
    for p in ("b1", "b2"):
        await _join(p, "team_b")
    await pool.match_pool("g", "pm", ["a1", "a2"], ["b1", "b2"])

    result = await pool.settle_pool("g", "pm", ["a1", "a2"])

    assert result["paid"] == {"a1": 100, "a2": 100}
    assert await pool.pool_balance("g", "pm") == 0
    balances = await wallet_service.balances_for("g", ["a1", "a2", "b1", "b2"])
    assert balances == {"a1": 250, "a2": 250, "b1": 150, "b2": 150}


@pytest.mark.asyncio
async def test_a_player_who_cannot_cover_the_fee_does_not_join(test_db):
    await wallet_service.adjust("g", "a1", 30, "seed", "t")

    with pytest.raises(AssertionError):
        await _join("a1", "team_a")

    assert await wallet_service.get_balance("g", "a1") == 30
    assert await pool.pool_balance("g", "pm") == 0
    async with db_session() as s:
        team_a = (await s.execute(select(DraftSession.team_a)
                  .where(DraftSession.session_id == "pm"))).scalars().first()
    assert not team_a, "an unfunded player was put on a team"


@pytest.mark.asyncio
async def test_a_free_premade_draft_charges_nothing(test_db):
    """Every premade that exists today has no fee, and must keep working."""
    async with db_session() as s:
        await s.execute(update(DraftSession)
                        .where(DraftSession.session_id == "pm")
                        .values(entry_fee=None))
    await wallet_service.adjust("g", "a1", 200, "seed", "t")

    await _join("a1", "team_a")

    assert await wallet_service.get_balance("g", "a1") == 200
    assert await pool.pool_balance("g", "pm") == 0


@pytest.mark.asyncio
async def test_an_entry_fee_draft_will_not_start_with_uneven_teams(test_db):
    """A fixed fee makes the sides level only while the teams are equal.

    Four players against three is 200 against 150, and levelling would hand
    part of a fee back to players who were told the fee was fixed -- 50 each
    becomes 40/40/40/30. Refusing to start is the honest answer: nobody is
    charged differently from what they agreed, and evening the teams up is
    something the players can just do.
    """
    from unittest.mock import AsyncMock, MagicMock

    import views

    async with db_session() as s:
        await s.execute(update(DraftSession)
                        .where(DraftSession.session_id == "pm")
                        .values(team_a=["a1", "a2"], team_b=["b1"],
                                sign_ups={"a1": "A1", "a2": "A2", "b1": "B1"},
                                team_a_name="Wolves", team_b_name="Bears"))

    view = views.PersistentView.__new__(views.PersistentView)
    interaction = MagicMock()
    said = []
    interaction.response.send_message = AsyncMock(
        side_effect=lambda c=None, **k: said.append(c))

    ok, _ = await views.PersistentView._validate_team_creation_request(
        view, interaction, "pm", "a1")

    assert ok is False, "an entry-fee draft started with 2 against 1"
    assert "Wolves" in said[0] and "Bears" in said[0], said
    assert "even teams" in said[0], said


@pytest.mark.asyncio
async def test_a_free_premade_draft_still_starts_with_uneven_teams(test_db):
    """The rule exists because of the fee. Without one, nothing is at stake and
    the draft is the players' business."""
    from unittest.mock import AsyncMock, MagicMock

    import views

    async with db_session() as s:
        await s.execute(update(DraftSession)
                        .where(DraftSession.session_id == "pm")
                        .values(entry_fee=None, team_a=["a1", "a2"], team_b=["b1"],
                                sign_ups={"a1": "A1", "a2": "A2", "b1": "B1"}))

    view = views.PersistentView.__new__(views.PersistentView)
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()

    ok, _ = await views.PersistentView._validate_team_creation_request(
        view, interaction, "pm", "a1")

    assert ok is True, "a free premade draft was blocked for uneven teams"


@pytest.mark.asyncio
async def test_settlement_reaches_a_premade_draft_that_holds_a_pool(test_db):
    """The gate used to be session_type == 'staked', which would have collected
    a premade draft's entries and never paid them out."""
    from models.match import MatchResult
    from utils import settle_decided_draft

    for p in ("a1", "b1"):
        await wallet_service.adjust("g", p, 200, "seed", "t")
    await _join("a1", "team_a")
    await _join("b1", "team_b")
    await pool.match_pool("g", "pm", ["a1"], ["b1"])
    async with db_session() as s:
        await s.execute(update(DraftSession)
                        .where(DraftSession.session_id == "pm")
                        .values(session_stage="pairings", match_counter=2))
        s.add(MatchResult(session_id="pm", match_number=1,
                          player1_id="a1", player1_wins=2,
                          player2_id="b1", player2_wins=0,
                          winner_id="a1", pairing_message_id="0"))

    await settle_decided_draft("pm")

    assert await pool.pool_balance("g", "pm") == 0, (
        "the premade draft's entries are still in the holder")
    balances = await wallet_service.balances_for("g", ["a1", "b1"])
    assert balances == {"a1": 250, "b1": 150}
