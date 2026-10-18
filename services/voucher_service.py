"""Draft vouchers: a wallet item worth a 20 tix entry, fully backed by tix.

Every outstanding voucher is backed by ``VOUCHER_VALUE`` tix held by ``RESERVE``.
Minting moves those tix from DraftBot's own wallet into the reserve, so the tix
system total never changes. Voucher counts live in the same ledger under
``VOUCHER``: ``MINT`` goes negative by the number minted, and ``RETIRED`` holds
vouchers whose draft has settled.
"""
import uuid
from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy import func, select

import config
from models.draft_session import DraftSession
from models.wallet_tx import TIX, VOUCHER, WalletTx
from database.db_session import db_session
from services import draft_pool_service, wallet_service

VOUCHER_VALUE = 20
RESERVE = "voucher:reserve"
MINT = "voucher:mint"
RETIRED = "voucher:retired"


# Said wherever a voucher entry's stake would be changed: it pays exactly 20 tix,
# so there is nothing to raise, lower or top up with the player's own tix.
CHANGE_REFUSAL = ("You entered with a voucher. To change your entry, cancel your "
                  "sign-up (your voucher comes back) and join again.")


class VoucherEntryRefused(RuntimeError):
    """The pool refused a voucher entry that passed every voucher check; the
    caller's transaction must roll back the voucher and reserve legs."""


def spent_holder(session_id: str) -> str:
    return f"voucher:spent:{session_id}"


@dataclass(frozen=True)
class Backing:
    """``free = reserve + in_play - VOUCHER_VALUE * outstanding``, and ``ok`` is ``free >= 0``.

    ``in_play`` is the tix that voucher entries still hold in their open pots: a
    voucher spent on a draft that has not settled is still outstanding, but its 20
    tix sit in the pot rather than in the reserve, and whatever the pot hands back
    is swept to the reserve. It never exceeds 20 per voucher in a draft, so
    ``free <= reserve`` always holds."""
    outstanding: int
    reserve: int
    free: int
    ok: bool
    in_play: int = 0


async def vouchers_held(guild_id: str, player_id: str) -> int:
    async with db_session() as session:
        return await wallet_service.balance_in(session, guild_id, player_id, asset=VOUCHER)


async def backing_in(session: Any, guild_id: str) -> Backing:
    minted = await wallet_service.balance_in(session, guild_id, MINT, asset=VOUCHER)
    retired = await wallet_service.balance_in(session, guild_id, RETIRED, asset=VOUCHER)
    outstanding = -(minted + retired)
    reserve = await wallet_service.balance_in(session, guild_id, RESERVE, asset=TIX)
    in_play = await _in_play_in(session, guild_id)
    free = reserve + in_play - VOUCHER_VALUE * outstanding
    return Backing(outstanding, reserve, free, free >= 0, in_play)


_SPENT_PREFIX = spent_holder("")


async def _in_play_in(session: Any, guild_id: str) -> int:
    """Tix that voucher entries still hold in the pots of unsettled drafts.

    A draft's spent holder keeps a positive balance until settlement retires it;
    each counterparty with a positive net into it entered with a voucher that is
    still spent (a returned voucher nets to zero)."""
    spent_rows = (await session.execute(
        select(WalletTx.player_id, WalletTx.counterparty_id)
        .where(WalletTx.guild_id == guild_id, WalletTx.asset == VOUCHER,
               WalletTx.player_id.like(f"{_SPENT_PREFIX}%"))
        .group_by(WalletTx.player_id, WalletTx.counterparty_id)
        .having(func.sum(WalletTx.amount) > 0))).all()
    total = 0
    for holder, player_id in spent_rows:
        session_id = holder[len(_SPENT_PREFIX):]
        if await wallet_service.balance_in(session, guild_id, holder, asset=VOUCHER) <= 0:
            # Settled: its vouchers are retired, and a loser's entry still nets
            # into the emptied pot, so it must not count as held.
            continue
        total += await draft_pool_service._held_in(session, guild_id, session_id, player_id)
    return total


async def backing(guild_id: str) -> Backing:
    async with db_session() as session:
        return await backing_in(session, guild_id)


async def mint_in(session: Any, guild_id: str, bot_id: str, to_player: str,
                  count: int, source: str) -> dict[str, int]:
    """Mint ``count`` vouchers to ``to_player``. The caller holds ``MONEY_LOCK``.

    Free reserve (reserve tix not backing an outstanding voucher) is used first;
    the bot pays only the remainder. Replaying a ``source`` books nothing."""
    if await wallet_service.transfer_legs(session, source, asset=VOUCHER):
        return {"from_free": 0, "from_bot": 0}
    need = VOUCHER_VALUE * count
    free = max((await backing_in(session, guild_id)).free, 0)
    from_free = min(free, need)
    from_bot = need - from_free
    if from_bot > 0:
        bot_balance = await wallet_service.balance_in(session, guild_id, bot_id, asset=TIX)
        if bot_balance < from_bot:
            raise wallet_service.InsufficientFunds(bot_id, from_bot, bot_balance)
        await wallet_service.transfer_in(
            session, guild_id, bot_id, RESERVE, from_bot, f"{source}:fund",
            "Draft voucher backing", asset=TIX)
    await wallet_service.transfer_in(
        session, guild_id, MINT, to_player, count, source, "Draft voucher", asset=VOUCHER)
    return {"from_free": from_free, "from_bot": from_bot}


async def return_free_in(session: Any, guild_id: str, bot_id: str, source: str) -> int:
    """Send all unbacked reserve tix back to the bot; returns the amount (0 is a no-op)."""
    free = (await backing_in(session, guild_id)).free
    if free <= 0:
        return 0
    await wallet_service.transfer_in(
        session, guild_id, RESERVE, bot_id, free, source,
        "Draft voucher reserve returned", asset=TIX)
    return free


async def give(guild_id: str, from_player: str, to_player: str, count: int) -> None:
    if count <= 0:
        raise ValueError("Voucher count must be positive")
    await wallet_service.pay(
        guild_id, from_player, to_player, count, asset=VOUCHER,
        source=f"voucher-give:{uuid.uuid4()}", notes="Draft voucher gift")


async def entered_with_voucher_in(session: Any, guild_id: str, session_id: str,
                                  player_id: str) -> bool:
    """Whether the player's entry in this draft is a voucher entry.

    True while the spent voucher sits in the draft's spent holder or after it has
    been retired from there; false once it has been returned to the player."""
    held = await wallet_service.net_between(
        session, guild_id, spent_holder(session_id), player_id, asset=VOUCHER)
    return held > 0


async def entered_with_voucher(guild_id: str, session_id: str, player_id: str) -> bool:
    async with db_session() as session:
        return await entered_with_voucher_in(session, guild_id, session_id, player_id)


async def enter_with_voucher_in(session: Any, guild_id: str, session_id: str,
                                player_id: str) -> str | None:
    """Enter the player for 20 tix paid from the reserve. The caller holds ``MONEY_LOCK``.

    Returns None on success, or a player-facing refusal (nothing written). The
    pot check doubles as the double-click guard: a second call under the lock
    sees the first entry and refuses."""
    if not config.vouchers_enabled(guild_id):
        return "Draft vouchers aren't available on this server."
    draft = (await session.execute(
        select(DraftSession).where(DraftSession.session_id == session_id))).scalars().first()
    if draft is None or draft.session_type != "staked" or draft.donation:
        return "Vouchers can only be used for staked drafts."
    if (draft.min_stake or 10) > VOUCHER_VALUE:
        return "This draft's minimum entry is above 20 tix, so a voucher can't cover it."
    if await wallet_service.balance_in(session, guild_id, player_id, asset=VOUCHER) < 1:
        return "You don't have a draft voucher."
    if await draft_pool_service._held_in(session, guild_id, session_id, player_id) > 0:
        return ("You're already entered in this draft. "
                "Cancel your sign-up first to use a voucher.")
    reserve = await wallet_service.balance_in(session, guild_id, RESERVE, asset=TIX)
    if reserve < VOUCHER_VALUE:
        logger.error(f"voucher reserve in guild {guild_id} holds {reserve} tix, "
                     f"short of one {VOUCHER_VALUE} tix entry for {player_id} in {session_id}")
        return "Vouchers can't be used right now. Please let an admin know."

    spent = spent_holder(session_id)
    moves = await wallet_service.movements_in(session, guild_id, spent, player_id, asset=VOUCHER)
    await wallet_service.transfer_in(
        session, guild_id, player_id, spent, 1,
        f"voucher-spend:{session_id}:{player_id}:{moves}", "Draft voucher entry",
        asset=VOUCHER)
    await wallet_service.transfer_in(
        session, guild_id, RESERVE, player_id, VOUCHER_VALUE,
        f"voucher-fund:{session_id}:{player_id}:{moves}", "Draft voucher entry",
        asset=TIX)
    charged = await draft_pool_service.entry_in(
        session, guild_id, session_id, player_id, VOUCHER_VALUE, "joined")
    if not charged["ok"]:
        raise VoucherEntryRefused(
            f"voucher entry for {player_id} in {session_id} was refused by the pool")
    return None


# --- the money coming back --------------------------------------------------
# A voucher entry's 20 tix reached the pot through the player's own wallet, so the
# pool pays refunds and winnings to the player like any other entry. These hooks
# run right after such a transfer, in the same transaction and under the same
# MONEY_LOCK, and sweep the voucher's share on to the reserve. They are plain
# transfers with no funds check, so a sweep cannot fail for a short wallet, and
# they never call on_inflow: a debt must not eat the reserve's share.

async def after_refund_in(session: Any, guild_id: str, session_id: str, player_id: str,
                          refunded: int, refund_source: str) -> None:
    """Sweep a refund of a voucher entry to the reserve; hand the voucher back
    once the player holds nothing in the pot. A no-op for any other entry."""
    if not await entered_with_voucher_in(session, guild_id, session_id, player_id):
        return
    await wallet_service.transfer_in(
        session, guild_id, player_id, RESERVE, refunded, f"voucher-sweep:{refund_source}",
        "Draft voucher entry returned", asset=TIX)
    if await draft_pool_service._held_in(session, guild_id, session_id, player_id) > 0:
        return
    spent = spent_holder(session_id)
    moves = await wallet_service.movements_in(session, guild_id, spent, player_id, asset=VOUCHER)
    await wallet_service.transfer_in(
        session, guild_id, spent, player_id, 1,
        f"voucher-return:{session_id}:{player_id}:{moves}", "Draft voucher returned",
        asset=VOUCHER)


async def after_payout_in(session: Any, guild_id: str, session_id: str, player_id: str,
                          credited: int, payout_source: str) -> int:
    """Sweep the voucher's half of a payout to the reserve; returns what the player
    keeps. A payout of 2m on a matched m keeps m, the winnings."""
    if not await entered_with_voucher_in(session, guild_id, session_id, player_id):
        return credited
    stake = credited // 2
    await wallet_service.transfer_in(
        session, guild_id, player_id, RESERVE, stake, f"voucher-sweep:{payout_source}",
        "Draft voucher entry returned", asset=TIX)
    return credited - stake


async def retire_in(session: Any, guild_id: str, session_id: str) -> int:
    """Retire every voucher spent on a settled draft; returns how many. Idempotent."""
    source = f"voucher-retire:{session_id}"
    spent = spent_holder(session_id)
    count = await wallet_service.balance_in(session, guild_id, spent, asset=VOUCHER)
    if count <= 0 or await wallet_service.transfer_legs(session, source, asset=VOUCHER):
        return 0
    await wallet_service.transfer_in(
        session, guild_id, spent, RETIRED, count, source, "Draft voucher used", asset=VOUCHER)
    return count
