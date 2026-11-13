"""Message content of the two escrow DMs: opened (a different sentence for each party)
and decided (the same sentence for both)."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import notification_service
from models.escrow_hold import RELEASED, RETURNED
from notification_service import notify_escrow_decided, notify_escrow_opened

GUILD_ID = "guild_1"
FUNDER = "funder_A"
COUNTERPARTY = "counter_B"
MOD = "mod_C"


def _make_bot():
    """A bot whose guild lookup yields no members, so names fall back to "User <id>"."""
    bot = MagicMock()
    guild = MagicMock()
    guild.get_member.return_value = None
    bot.get_guild.return_value = guild
    return bot


def _sent(mock_send):
    return [(c.args[1], c.args[2]) for c in mock_send.await_args_list]


async def _opened(recipient, note="cube deposit", send_result=True):
    with patch.object(notification_service, "send_dm",
                      new=AsyncMock(return_value=send_result)) as send:
        await notify_escrow_opened(_make_bot(), GUILD_ID, recipient, escrow_id=7,
                                   funder_id=FUNDER, counterparty_id=COUNTERPARTY,
                                   amount=40, note=note)
    return _sent(send)


async def _decided(recipient, outcome, send_result=True, note="cube deposit"):
    with patch.object(notification_service, "send_dm",
                      new=AsyncMock(return_value=send_result)) as send:
        await notify_escrow_decided(_make_bot(), GUILD_ID, recipient, escrow_id=7,
                                    funder_id=FUNDER, counterparty_id=COUNTERPARTY,
                                    amount=40, outcome=outcome, decided_by=MOD, note=note)
    return _sent(send)


@pytest.mark.asyncio
async def test_opened_to_funder_names_the_counterparty():
    (recipient, msg), = await _opened(FUNDER)
    assert recipient == FUNDER
    assert msg == (f"🔒 Escrow #7: **40 tix** from your wallet are held for User {COUNTERPARTY}. "
                   "A moderator will release or return them. Note: cube deposit")


@pytest.mark.asyncio
async def test_opened_to_counterparty_names_the_funder():
    (recipient, msg), = await _opened(COUNTERPARTY)
    assert recipient == COUNTERPARTY
    assert msg == (f"🔒 Escrow #7: User {FUNDER} put **40 tix** in escrow for you. "
                   "A moderator will release or return them. Note: cube deposit")


@pytest.mark.asyncio
async def test_released_names_counterparty_and_moderator():
    for recipient in (FUNDER, COUNTERPARTY):
        (to, msg), = await _decided(recipient, RELEASED)
        assert to == recipient
        assert msg == f"✅ Escrow #7: **40 tix** released to User {COUNTERPARTY} by User {MOD}. Note: cube deposit"


@pytest.mark.asyncio
async def test_returned_names_funder_and_moderator():
    for recipient in (FUNDER, COUNTERPARTY):
        (to, msg), = await _decided(recipient, RETURNED)
        assert to == recipient
        assert msg == f"↩️ Escrow #7: **40 tix** returned to User {FUNDER} by User {MOD}. Note: cube deposit"


@pytest.mark.asyncio
async def test_send_dm_returning_false_is_not_an_error():
    assert len(await _opened(FUNDER, send_result=False)) == 1
    assert len(await _decided(FUNDER, RELEASED, send_result=False)) == 1


@pytest.mark.asyncio
async def test_a_note_cannot_ping():
    note = "@everyone @here <@123456789012345678> <@&789012345678901234> pay up"
    for recipient in (FUNDER, COUNTERPARTY):
        (_, msg), = await _opened(recipient, note=note)
        assert "@everyone" not in msg
        assert "@here" not in msg
        assert "<@123456789012345678>" not in msg
        assert "<@&789012345678901234>" not in msg
        assert "pay up" in msg
        # escape_mentions breaks the token with a zero-width space instead of dropping it.
        assert "@\u200beveryone" in msg


@pytest.mark.asyncio
async def test_a_decided_note_cannot_ping():
    note = "@everyone <@123456789012345678> pay up"
    for outcome in (RELEASED, RETURNED):
        (_, msg), = await _decided(FUNDER, outcome, note=note)
        assert "@everyone" not in msg
        assert "<@123456789012345678>" not in msg
        assert "@\u200beveryone" in msg and "pay up" in msg


@pytest.mark.asyncio
async def test_a_note_cannot_restyle_the_message():
    """The note is one player's words shown to another, so its markdown is shown, not
    obeyed, and it stays on one line."""
    with patch("notification_service.send_dm", new=AsyncMock(return_value=True)) as dm:
        await notification_service.notify_escrow_opened(
            MagicMock(), "g", "222", escrow_id=3, funder_id="111", counterparty_id="222",
            amount=5, note="**big**\n# heading")
    msg = dm.await_args.args[2]
    assert r"\*\*big\*\*" in msg
    assert "\n" not in msg
