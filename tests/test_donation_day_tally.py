"""The donation page's arithmetic, and that it never leaks an id."""
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from database.db_session import db_session
from services import debt_service, wallet_service
from services.donation_day import Credit, build_payload, gather_credits, slugify, tally
from services.donation_service import DONATION_SOURCE, mark_paid_cash

T0 = datetime(2026, 10, 16, 12, 0)
EVENT = {"name": "Donation Day", "goal_usd": 500, "starts_at": T0.isoformat(),
         "page_key": "donation-day", "active": True}
BOT = "999999999999999999"
WINNER, LOSER, GIVER = "111111111111111111", "222222222222222222", "333333333333333333"


def test_winners_and_payers_are_both_credited_but_money_counts_once():
    credits = [Credit(WINNER, 50, T0, "won"), Credit(LOSER, 50, T0, "paid")]

    payload = tally(credits, 30, {WINNER: "Ann", LOSER: "Cat"}, set(), EVENT, T0)

    assert payload["raised"] == 50 and payload["pledged"] == 80 and payload["goal"] == 500
    assert {r["name"]: (r["won"], r["paid"], r["total"]) for r in payload["rows"]} == \
        {"Ann": (50, 0, 50), "Cat": (0, 50, 50)}


def test_rows_rank_by_total():
    credits = [Credit(WINNER, 20, T0, "won"), Credit(LOSER, 90, T0, "paid")]

    payload = tally(credits, 0, {WINNER: "Ann", LOSER: "Cat"}, set(), EVENT, T0)

    assert [r["name"] for r in payload["rows"]] == ["Cat", "Ann"]


def test_an_anonymous_donor_still_counts_and_ranks():
    credits = [Credit(LOSER, 90, T0, "paid"), Credit(WINNER, 20, T0, "won")]

    payload = tally(credits, 0, {WINNER: "Ann", LOSER: "Cat"}, {LOSER}, EVENT, T0)

    assert payload["rows"][0] == {"name": "Anonymous", "won": 0, "paid": 90, "total": 90}
    assert payload["raised"] == 90
    assert all(item["name"] != "Cat" for item in payload["recent"])


def test_no_discord_id_ever_reaches_the_page():
    credits = [Credit(WINNER, 50, T0, "won"), Credit(LOSER, 50, T0, "paid")]

    text = json.dumps(tally(credits, 0, {}, set(), EVENT, T0))

    assert WINNER not in text and LOSER not in text


def test_slugs_are_url_safe():
    assert slugify("Donation Day — Oct 16!") == "donation-day-oct-16"
    assert slugify("!!!") == "donation-day"


@pytest.mark.asyncio
async def test_direct_gifts_count_only_from_the_start_and_auto_draws_count_once(test_db):
    # A donation debt, auto-drawn from the loser's wallet (a settlement row PLUS
    # a debt:<id> wallet transfer -- which must not count a second time).
    await debt_service.create_ledger_entries("g", LOSER, BOT, 50, DONATION_SOURCE,
                                             f"donation:d1:{LOSER}")
    await wallet_service.adjust("g", LOSER, 50, "seed", "t")
    from services.mtgo_resolution_service import settle_debt_from_wallet
    await settle_debt_from_wallet("g", LOSER, BOT, 50)
    # A direct gift through /wallet pay's transfer.
    await wallet_service.adjust("g", GIVER, 25, "seed", "t")
    await wallet_service.pay("g", GIVER, BOT, 25)

    async with db_session() as s:
        credits, outstanding = await gather_credits(s, "g", BOT, datetime.now() - timedelta(minutes=5))
        later, _ = await gather_credits(s, "g", BOT, datetime.now() + timedelta(minutes=5))

    assert sorted((c.player_id, c.amount, c.kind) for c in credits) == \
        sorted([(LOSER, 50, "paid"), (GIVER, 25, "paid")])
    assert outstanding == 0
    assert later == []


@pytest.mark.asyncio
async def test_a_cash_mark_counts_as_paid_and_an_unpaid_debt_as_pledged(test_db):
    for player in (LOSER, GIVER):
        await debt_service.create_ledger_entries("g", player, BOT, 40, DONATION_SOURCE,
                                                 f"donation:d1:{player}")
    await mark_paid_cash("g", LOSER, BOT)

    with patch("services.donation_day.get_anonymous_donors", AsyncMock(return_value=set())):
        payload = await build_payload(
            "g", {**EVENT, "starts_at": (datetime.now() - timedelta(minutes=5)).isoformat()}, BOT)

    assert payload["raised"] == 40 and payload["pledged"] == 80


@pytest.mark.asyncio
async def test_the_winners_of_a_settled_donation_draft_are_credited_their_shares(test_db):
    import services.donation_service as donation
    from conftest import seed_session, seed_stakes
    from utils import settle_decided_draft

    teams = (["a1", "a2"], ["b1", "b2"])
    await seed_session("d1", guild="g", stage="pairings", donation=True, teams=teams,
                       matches=[("a1", "b1", "a1", None)], match_counter=2)
    await seed_stakes("d1", {"a1": (100, False), "a2": (50, False),
                             "b1": (50, False), "b2": (50, False)})
    await donation.level_donation("d1", *teams)
    with patch.object(donation, "draftbot_id", return_value=BOT), \
         patch("notification_service.notify_wallet", AsyncMock()):
        await settle_decided_draft("d1")

    async with db_session() as s:
        credits, outstanding = await gather_credits(
            s, "g", BOT, datetime.now() - timedelta(minutes=5))

    assert sorted((c.player_id, c.amount, c.kind) for c in credits) == \
        [("a1", 50, "won"), ("a2", 50, "won")]
    assert outstanding == 100
