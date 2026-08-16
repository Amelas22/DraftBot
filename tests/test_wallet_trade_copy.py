"""What the wallet tells a player to do next.

Two things had gone wrong in the copy. `/wallet pay` on an empty wallet reported
"Insufficient funds: 123456789012345678 needs 5, has 0" — the player's own Discord id
read back at them, and no hint that depositing is the fix. And `/wallet deposit` told
them to open an MTGO trade themselves, which is not how the serve works: the custodian
messages the player, they reply YES, and it opens the trade. Withdraw described the
same protocol differently again.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers.money_gate import mtgo_trade_prompt


async def _run_trade(command, start_name):
    """Drive deposit/withdraw to the point where they tell the player what to do."""
    from cogs.wallet_cog import WalletCommands

    cog = WalletCommands.__new__(WalletCommands)
    cog.bot = MagicMock()   # the followup task captures it; nothing here runs that task
    ctx = _ctx()

    with patch("cogs.wallet_cog.gate_serve", return_value=None), \
         patch("cogs.wallet_cog.linked_username", new=AsyncMock(return_value="me")), \
         patch("cogs.wallet_cog.custodian_name", new=AsyncMock(return_value="TheCustodian")), \
         patch(f"cogs.wallet_cog.resolution.{start_name}",
               new=AsyncMock(return_value={"ok": True, "job_id": "J1"})), \
         patch("cogs.wallet_cog.spawn_followup",
               side_effect=lambda label, coro: coro.close()):
        await command.callback(cog, ctx, 5)

    return ctx.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_both_trade_commands_give_the_same_instructions():
    """Runs both commands and compares what they actually send.

    Deposit and withdraw drive the same serve and take the same steps, but they drifted
    once already — deposit told the player to open the trade themselves, which the serve
    never asks for. Asserting on the sent message rather than on the source means a
    command that imports the shared prompt and then fails to send it still fails.
    """
    from cogs.wallet_cog import WalletCommands

    deposit_msg = await _run_trade(WalletCommands.wallet_deposit, "start_deposit")
    withdraw_msg = await _run_trade(WalletCommands.wallet_withdraw, "start_withdraw")

    shared = mtgo_trade_prompt("TheCustodian")
    assert shared in deposit_msg, "deposit should render the shared instructions"
    assert shared in withdraw_msg, "withdraw should render the shared instructions"


def test_the_prompt_says_to_reply_yes_and_wait():
    prompt = mtgo_trade_prompt("TheCustodian")

    assert "YES" in prompt, "replying YES is the step players miss"
    assert "TheCustodian" in prompt
    # the serve opens the trade; telling players to open it themselves is what the
    # old deposit copy got wrong
    assert "will open" in prompt


# ---- /wallet pay on an empty wallet ----------------------------------------------


def _ctx(author_id=111, guild_id=999):
    ctx = MagicMock()
    ctx.author.id = author_id
    ctx.guild.id = guild_id
    ctx.defer = AsyncMock()
    ctx.followup.send = AsyncMock()
    return ctx


async def _run_pay(pay_result, amount=5):
    """Drive wallet_pay far enough to reach the failure branch."""
    from cogs.wallet_cog import WalletCommands

    cog = WalletCommands.__new__(WalletCommands)
    ctx = _ctx()
    target = MagicMock()
    target.id = 222
    target.display_name = "Someone"

    with patch("cogs.wallet_cog.gate_read", return_value=None), \
         patch("cogs.wallet_cog.MtgoAccount.usernames_for_discord_ids",
               new=AsyncMock(return_value={"111": "me", "222": "them"})), \
         patch("cogs.wallet_cog.resolution.pay", new=AsyncMock(return_value=pay_result)):
        await WalletCommands.wallet_pay.callback(cog, ctx, target, amount)

    return ctx.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_paying_with_an_empty_wallet_points_at_deposit():
    msg = await _run_pay(
        {"ok": False, "error": "Insufficient funds: 111 needs 5, has 0",
         "code": "insufficient_funds", "available": 0})

    assert "/wallet deposit" in msg
    assert "0 tix" in msg
    # the raw service string names the player by Discord id; players should never see it
    assert "111" not in msg


@pytest.mark.asyncio
async def test_the_deposit_advice_names_everything_that_gets_paid_first():
    """Tells the player where an arriving deposit goes, rather than setting them
    arithmetic.

    settle_deposit_inflow spends a deposit on a pending tournament entry first and
    debts second. Someone with either who is told only "deposit more" would trade tix
    in, watch them vanish into a creditor, and have no idea why — so both claims are
    named, in the order they are actually paid.
    """
    msg = await _run_pay(
        {"ok": False, "error": "...", "code": "insufficient_funds", "available": 2})

    assert "tournament entry" in msg.lower()
    assert "debts" in msg.lower()
    assert "/debts summary" in msg


@pytest.mark.asyncio
async def test_other_failures_do_not_suggest_depositing():
    """Depositing does not fix "amount must be positive". Only the funds case earns
    the advice, which is why the service tags it rather than the cog matching prose."""
    msg = await _run_pay({"ok": False, "error": "cannot pay yourself"})

    assert "deposit" not in msg.lower()
    assert "cannot pay yourself" in msg


def test_the_funds_failure_is_its_own_type():
    """A ValueError subclass, so every existing `except ValueError` still catches it,
    but distinguishable for callers that answer it differently.

    Only `available` is pinned: it is the one thing a caller cannot work out for
    itself, and asserting on attributes nobody reads would present them as contract."""
    from services.wallet_service import InsufficientFunds

    err = InsufficientFunds("111", 5, 2)
    assert isinstance(err, ValueError)
    assert err.available == 2


@pytest.mark.asyncio
async def test_the_funds_failure_reaches_the_cog_tagged():
    """The tests above hand the cog a ready-made result, so they pin the cog's half of
    the contract and nothing else. This pins the other half: that resolution.pay really
    does turn the exception into the code the cog switches on. Without it, dropping the
    tag would silently fall back to the raw "needs 5, has 0" string with every other
    test still green.
    """
    from services import mtgo_resolution_service as resolution
    from services import wallet_service
    from services.wallet_service import InsufficientFunds

    with patch("services.wallet_service.pay",
               new=AsyncMock(side_effect=InsufficientFunds("111", 5, 2))):
        res = await resolution.pay("999", "111", "222", 5)

    assert res["ok"] is False
    assert res["code"] == wallet_service.INSUFFICIENT_FUNDS
    assert res["available"] == 2


@pytest.mark.asyncio
async def test_other_value_errors_stay_untagged():
    from services import mtgo_resolution_service as resolution

    with patch("services.wallet_service.pay",
               new=AsyncMock(side_effect=ValueError("Cannot transfer to the same holder"))):
        res = await resolution.pay("999", "111", "111", 5)

    assert res["ok"] is False
    assert "code" not in res
