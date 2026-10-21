# tests/test_donation_teams.py
"""Forming teams on a donation draft records shares and moves no money."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select

import services.team_creator as team_creator
from conftest import seed_session, seed_stakes
from database.db_session import db_session
from models.wallet_tx import WalletTx
from services.donation_service import donation_shares, level_donation
from session import get_draft_session

PLAYERS = {"a1": "Ann", "a2": "Ben", "b1": "Cat", "b2": "Dan"}
BETS = {"a1": (100, False), "a2": (50, False), "b1": (50, False), "b2": (50, False)}


async def _wallet_rows() -> int:
    async with db_session() as s:
        return (await s.execute(select(func.count()).select_from(WalletTx))).scalar()


@pytest.mark.asyncio
async def test_levelling_a_donation_draft_records_shares_and_moves_no_money(test_db):
    await seed_session("d1", guild="g", stage="teams", donation=True,
                       sign_ups=dict(PLAYERS), teams=(["a1", "a2"], ["b1", "b2"]))
    await seed_stakes("d1", BETS)

    await level_donation("d1", ["a1", "a2"], ["b1", "b2"])

    assert await donation_shares("d1") == {"a1": 50, "a2": 50, "b1": 50, "b2": 50}
    assert await _wallet_rows() == 0


@pytest.mark.asyncio
async def test_levelling_twice_records_the_same_shares(test_db):
    await seed_session("d1", guild="g", stage="teams", donation=True,
                       sign_ups=dict(PLAYERS), teams=(["a1", "a2"], ["b1", "b2"]))
    await seed_stakes("d1", BETS)

    await level_donation("d1", ["a1", "a2"], ["b1", "b2"])
    await level_donation("d1", ["a1", "a2"], ["b1", "b2"])

    assert await donation_shares("d1") == {"a1": 50, "a2": 50, "b1": 50, "b2": 50}


def _interaction():
    interaction = MagicMock()
    interaction.guild_id = 1
    interaction.message = SimpleNamespace(id=0)
    interaction.followup.send = AsyncMock()
    interaction.followup.edit_message = AsyncMock()
    interaction.channel.send = AsyncMock()
    return interaction


async def _donation_queue():
    await seed_session("d1", guild="1", stype="staked", stage=None, donation=True,
                       sign_ups=dict(PLAYERS),
                       deletion_time=datetime.now() + timedelta(minutes=90))
    await seed_stakes("d1", BETS)


def _view():
    view = MagicMock()
    view.session_type = "staked"
    view.children = []
    return view


@pytest.mark.asyncio
async def test_create_teams_records_shares_for_a_donation_draft(test_db):
    await _donation_queue()
    match_pool = AsyncMock()
    with patch.object(team_creator, "match_pool", match_pool), \
         patch.object(team_creator, "_create_teams_embed", AsyncMock()), \
         patch.object(team_creator, "_create_channel_announcement_embed", AsyncMock()), \
         patch.object(team_creator, "_handle_staked_draft_completion", AsyncMock()), \
         patch("ready_check.ReadyCheckSession.cleanup", AsyncMock()):
        created = await team_creator.create_and_display_teams(
            MagicMock(), "d1", _interaction(), _view())

    assert created is True
    match_pool.assert_not_awaited()
    assert sum((await donation_shares("d1")).values()) > 0
    assert await _wallet_rows() == 0


@pytest.mark.asyncio
async def test_a_donation_draft_whose_shares_cannot_be_recorded_returns_to_sign_ups(test_db):
    await _donation_queue()
    interaction = _interaction()
    with patch.object(team_creator, "level_donation",
                      AsyncMock(side_effect=RuntimeError("db gone"))), \
         patch("ready_check.ReadyCheckSession.cleanup", AsyncMock()):
        created = await team_creator.create_and_display_teams(
            MagicMock(), "d1", interaction, _view())

    assert created is False
    row = await get_draft_session("d1")
    assert row.session_stage is None and row.team_a is None
    said = " ".join(str(c.args[0]) for c in interaction.followup.send.await_args_list)
    assert "Create Teams" in said
    interaction.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_donation_draft_holds_a_capped_entry_to_the_draft_minimum(test_db):
    """50 beside three teammates on 20 is 45% of its side, which the share
    alone would let stand; on a team of minimum entries the cap holds it to 20."""
    from sqlalchemy import update
    from models.draft_session import DraftSession

    team_a, team_b = ["a1", "a2", "a3", "a4"], ["b1", "b2", "b3", "b4"]
    await seed_session("d1", guild="g", stage="teams", donation=True,
                       sign_ups={p: p for p in team_a + team_b},
                       teams=(team_a, team_b))
    async with db_session() as s:
        await s.execute(update(DraftSession).where(DraftSession.session_id == "d1")
                        .values(min_stake=20))
    await seed_stakes("d1", {**{p: (50, False) for p in team_a},
                             "b1": (50, True), "b2": (20, False),
                             "b3": (20, False), "b4": (20, False)})

    await level_donation("d1", team_a, team_b)

    assert await donation_shares("d1") == {p: 20 for p in team_a + team_b}
