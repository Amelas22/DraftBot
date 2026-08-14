"""
Unit tests for wallet movement DM notifications.

Covers the message content of the wallet notifiers, and the contract that matters
most in money code: a failing DM must never propagate out of a money path. The
transfer has already committed by the time these run.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import notification_service
from notification_service import (
    notify_auto_settlement,
    notify_entry_refund,
    notify_payment_received,
    notify_tournament_payout,
)

GUILD_ID = "guild_1"
PAYER_ID = "payer_A"
CREDITOR_ID = "creditor_B"


def _make_bot():
    """A bot whose guild lookup yields no members, so names fall back to "User <id>"."""
    bot = MagicMock()
    guild = MagicMock()
    guild.get_member.return_value = None
    bot.get_guild.return_value = guild
    return bot


def _sent(mock_send):
    """[(user_id, message)] for each send_dm call."""
    return [(c.args[1], c.args[2]) for c in mock_send.await_args_list]


@pytest.mark.asyncio
async def test_payment_received_names_the_sender_and_amount():
    with patch.object(notification_service, "send_dm", new=AsyncMock(return_value=True)) as send:
        await notify_payment_received(_make_bot(), GUILD_ID, CREDITOR_ID, PAYER_ID, 25)
    (recipient, msg), = _sent(send)
    assert recipient == CREDITOR_ID
    assert "25 tix" in msg
    assert PAYER_ID in msg          # falls back to "User payer_A"


@pytest.mark.asyncio
async def test_payment_received_includes_the_note_when_given():
    with patch.object(notification_service, "send_dm", new=AsyncMock(return_value=True)) as send:
        await notify_payment_received(_make_bot(), GUILD_ID, CREDITOR_ID, PAYER_ID, 5,
                                      note="draft payout")
    (_, msg), = _sent(send)
    assert "draft payout" in msg


@pytest.mark.asyncio
async def test_auto_settlement_tells_BOTH_parties():
    """Neither side initiates an auto-draw, so both have to be told."""
    with patch.object(notification_service, "send_dm", new=AsyncMock(return_value=True)) as send:
        await notify_auto_settlement(_make_bot(), GUILD_ID, PAYER_ID, CREDITOR_ID, 30,
                                     remaining=20)
    sent = _sent(send)
    assert {u for u, _ in sent} == {PAYER_ID, CREDITOR_ID}
    payer_msg = next(m for u, m in sent if u == PAYER_ID)
    creditor_msg = next(m for u, m in sent if u == CREDITOR_ID)
    assert "30 tix" in payer_msg and "30 tix" in creditor_msg
    assert "still owe them" in payer_msg
    assert "still owe you" in creditor_msg


@pytest.mark.asyncio
async def test_auto_settlement_reports_a_cleared_debt():
    with patch.object(notification_service, "send_dm", new=AsyncMock(return_value=True)) as send:
        await notify_auto_settlement(_make_bot(), GUILD_ID, PAYER_ID, CREDITOR_ID, 30,
                                     remaining=0)
    sent = dict(_sent(send))
    assert "clears your debt" in sent[PAYER_ID]
    assert "fully settled" in sent[CREDITOR_ID]


@pytest.mark.asyncio
async def test_payout_and_refund_messages():
    with patch.object(notification_service, "send_dm", new=AsyncMock(return_value=True)) as send:
        await notify_tournament_payout(_make_bot(), GUILD_ID, PAYER_ID, 100, place=1,
                                       tournament_name="Team Rocket")
        await notify_entry_refund(_make_bot(), GUILD_ID, PAYER_ID, 20, team_name="Team Rocket")
    payout, refund = [m for _, m in _sent(send)]
    assert "100 tix" in payout and "place 1" in payout and "Team Rocket" in payout
    assert "20 tix" in refund and "refunded" in refund


@pytest.mark.asyncio
async def test_a_failing_dm_never_propagates():
    """The money has already moved; a DM failure must not surface as an error."""
    boom = AsyncMock(side_effect=RuntimeError("discord is down"))
    with patch.object(notification_service, "send_dm", new=boom):
        await notify_payment_received(_make_bot(), GUILD_ID, CREDITOR_ID, PAYER_ID, 25)
        await notify_auto_settlement(_make_bot(), GUILD_ID, PAYER_ID, CREDITOR_ID, 5, remaining=0)
        await notify_tournament_payout(_make_bot(), GUILD_ID, PAYER_ID, 100)
        await notify_entry_refund(_make_bot(), GUILD_ID, PAYER_ID, 20)


@pytest.mark.asyncio
async def test_names_survive_a_bot_with_no_guild():
    """Services run outside guild context; an uncached guild must not break the DM."""
    bot = MagicMock()
    bot.get_guild.return_value = None
    with patch.object(notification_service, "send_dm", new=AsyncMock(return_value=True)) as send:
        await notify_payment_received(bot, GUILD_ID, CREDITOR_ID, PAYER_ID, 25)
    (_, msg), = _sent(send)
    assert "25 tix" in msg


@pytest.mark.asyncio
async def test_notify_wallet_is_a_no_op_without_a_registered_bot():
    """Tests, migrations and CLI runs have no bot; the money path must not care."""
    from services.mtgo_resolution_service import _notify_wallet
    with patch("bot_registry.get_bot", return_value=None):
        await _notify_wallet("notify_payment_received", GUILD_ID, CREDITOR_ID, PAYER_ID, 25)


@pytest.mark.asyncio
async def test_notify_wallet_swallows_a_broken_notifier():
    from services.mtgo_resolution_service import _notify_wallet
    with patch("bot_registry.get_bot", return_value=MagicMock()), \
         patch.object(notification_service, "notify_payment_received",
                      new=AsyncMock(side_effect=RuntimeError("boom"))):
        await _notify_wallet("notify_payment_received", GUILD_ID, CREDITOR_ID, PAYER_ID, 25)
