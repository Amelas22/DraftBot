"""The refusals /wallet pay gives before and when a transfer cannot happen.

They are shared with the escrow commands, so the wording is pinned here: a change to
either command's text has to be a deliberate change to both.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.wallet_cog import WalletCommands

SENDER_UNLINKED = "Link your MTGO account first with `/link_mtgo`."
RECIPIENT_UNLINKED = ("Ada hasn't linked an MTGO account yet, so they can't "
                      "receive tix. Ask them to run `/link_mtgo` first.")


def _short(have, amount, verb):
    return (f"You have **{have} tix** — {verb} **{amount}** needs "
            f"**{amount - have}** more.\n"
            f"Use `/wallet deposit` to trade tix in from MTGO. Deposits "
            f"automatically cover any pending tournament entry first, then any "
            f"outstanding debts — `/debts summary` shows what you owe.")


async def _pay_reply(linked, pay_result=None, amount=5):
    cog = WalletCommands.__new__(WalletCommands)
    ctx = MagicMock()
    ctx.author.id = 111
    ctx.guild.id = 999
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    target = MagicMock()
    target.id = 222
    target.display_name = "Ada"
    with patch("cogs.wallet_cog.gate_read", return_value=None), \
         patch("cogs.wallet_cog.MtgoAccount.usernames_for_discord_ids",
               new=AsyncMock(return_value=linked)), \
         patch("cogs.wallet_cog.resolution.pay", new=AsyncMock(return_value=pay_result)):
        await WalletCommands.wallet_pay.callback(cog, ctx, target, amount)
    return ctx.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_pay_tells_an_unlinked_sender_to_link():
    assert await _pay_reply({"222": "them"}) == SENDER_UNLINKED


@pytest.mark.asyncio
async def test_pay_names_an_unlinked_recipient():
    assert await _pay_reply({"111": "me"}) == RECIPIENT_UNLINKED


@pytest.mark.asyncio
async def test_pay_asks_a_sender_who_is_neither_linked_to_link_first():
    assert await _pay_reply({}) == SENDER_UNLINKED


@pytest.mark.asyncio
async def test_pay_short_of_funds_message_is_exact():
    msg = await _pay_reply(
        {"111": "me", "222": "them"},
        {"ok": False, "code": "insufficient_funds", "available": 2}, amount=5)
    assert msg == _short(2, 5, "sending")


# ---- the helpers themselves -------------------------------------------------------


def _member(member_id, name):
    member = MagicMock()
    member.id = member_id
    member.display_name = name
    return member


async def _refusal(linked):
    from helpers.money_gate import unlinked_refusal
    with patch("helpers.money_gate.MtgoAccount.usernames_for_discord_ids",
               new=AsyncMock(return_value=linked)) as lookup:
        out = await unlinked_refusal(_member(111, "Me"), _member(222, "Ada"))
    lookup.assert_awaited_once_with([111, 222])
    return out


@pytest.mark.asyncio
async def test_unlinked_refusal_is_none_when_both_are_linked():
    assert await _refusal({"111": "me", "222": "them"}) is None


@pytest.mark.asyncio
async def test_unlinked_refusal_checks_the_sender_first():
    assert await _refusal({}) == SENDER_UNLINKED
    assert await _refusal({"222": "them"}) == SENDER_UNLINKED


@pytest.mark.asyncio
async def test_unlinked_refusal_names_the_recipient():
    assert await _refusal({"111": "me"}) == RECIPIENT_UNLINKED


def test_short_funds_message_takes_the_verb_and_keeps_the_rest():
    from helpers.money_gate import short_funds_message
    assert short_funds_message(2, 5, "sending") == _short(2, 5, "sending")
    holding = short_funds_message(40, 100, "holding")
    assert holding.startswith("You have **40 tix** — holding **100** needs **60** more.\n")
    assert holding.endswith(_short(40, 100, "sending").split("\n", 1)[1])
