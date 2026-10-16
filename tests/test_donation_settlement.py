"""A decided donation draft leaves its losers owing DraftBot their shares."""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

import services.donation_service as donation
from conftest import seed_session, seed_stakes
from database.db_session import db_session
from models.debt_ledger import DebtLedger
from services import debt_service, wallet_service
from utils import settle_decided_draft

BOT = "999"
TEAMS = (["101", "102"], ["201", "202"])
BETS = {"101": (100, False), "102": (50, False), "201": (50, False), "202": (50, False)}
A_WINS = [("101", "201", "101", None)]                               # 1 of 1 match
DRAWN = [("101", "201", "101", None), ("102", "202", "202", None)]      # 1-1 of 2


async def _donation_draft(matches, *, level=True):
    await seed_session("d1", guild="g", stage="pairings", donation=True,
                       teams=TEAMS, matches=matches,
                       match_counter=len(matches) + 1)
    await seed_stakes("d1", BETS)
    if level:
        await donation.level_donation("d1", *TEAMS)


async def _donation_rows():
    async with db_session() as s:
        return (await s.execute(select(DebtLedger).where(
            DebtLedger.source_type == "donation"))).scalars().all()


@pytest.fixture(autouse=True)
def _quiet():
    with patch.object(donation, "draftbot_id", return_value=BOT), \
         patch("notification_service.notify_wallet", AsyncMock()):
        yield


@pytest.mark.asyncio
async def test_losers_owe_draftbot_their_shares_and_a_funded_loser_is_drawn(test_db):
    await _donation_draft(A_WINS)
    await wallet_service.adjust("g", "201", 200, "seed", "test")

    await settle_decided_draft("d1")

    assert await wallet_service.get_balance("g", "201") == 150
    assert await wallet_service.get_balance("g", BOT) == 50
    assert await debt_service.get_balance_with("g", "201", BOT) == 0
    assert await debt_service.get_balance_with("g", "202", BOT) == -50, \
        "an unfunded loser keeps the debt"
    assert await debt_service.get_balance_with("g", "101", BOT) == 0, \
        "a winner owes nothing"


@pytest.mark.asyncio
async def test_each_debt_names_the_draft_and_the_loser(test_db):
    await _donation_draft(A_WINS)

    await settle_decided_draft("d1")

    debtor_rows = sorted((r.player_id, r.source_id, r.amount)
                         for r in await _donation_rows() if r.amount < 0)
    assert debtor_rows == [("201", "donation:d1:201", -50), ("202", "donation:d1:202", -50)]


@pytest.mark.asyncio
async def test_a_draw_books_nothing(test_db):
    await _donation_draft(DRAWN)

    await settle_decided_draft("d1")

    assert await _donation_rows() == []


@pytest.mark.asyncio
async def test_a_replay_books_nothing_twice(test_db):
    await _donation_draft(A_WINS)
    await wallet_service.adjust("g", "201", 200, "seed", "test")

    await settle_decided_draft("d1")
    await settle_decided_draft("d1")

    assert len(await _donation_rows()) == 4
    assert await wallet_service.get_balance("g", "201") == 150


@pytest.mark.asyncio
async def test_a_draft_whose_shares_were_never_recorded_still_settles(test_db):
    """A crash between committing the teams and recording the shares."""
    await _donation_draft(A_WINS, level=False)

    await settle_decided_draft("d1")

    assert await debt_service.get_balance_with("g", "202", BOT) == -50


@pytest.mark.asyncio
async def test_without_draftbots_id_nothing_is_booked_and_the_next_report_retries(test_db):
    await _donation_draft(A_WINS)

    with patch.object(donation, "draftbot_id", return_value=None):
        await settle_decided_draft("d1")
    assert await _donation_rows() == []

    await settle_decided_draft("d1")
    assert len(await _donation_rows()) == 4
