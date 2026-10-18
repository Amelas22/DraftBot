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

from models.wallet_tx import TIX, VOUCHER
from database.db_session import db_session
from services import wallet_service

VOUCHER_VALUE = 20
RESERVE = "voucher:reserve"
MINT = "voucher:mint"
RETIRED = "voucher:retired"


def spent_holder(session_id: str) -> str:
    return f"voucher:spent:{session_id}"


@dataclass(frozen=True)
class Backing:
    outstanding: int
    reserve: int
    free: int
    ok: bool


async def vouchers_held(guild_id: str, player_id: str) -> int:
    async with db_session() as session:
        return await wallet_service.balance_in(session, guild_id, player_id, asset=VOUCHER)


async def backing_in(session: Any, guild_id: str) -> Backing:
    minted = await wallet_service.balance_in(session, guild_id, MINT, asset=VOUCHER)
    retired = await wallet_service.balance_in(session, guild_id, RETIRED, asset=VOUCHER)
    outstanding = -(minted + retired)
    reserve = await wallet_service.balance_in(session, guild_id, RESERVE, asset=TIX)
    free = reserve - VOUCHER_VALUE * outstanding
    return Backing(outstanding, reserve, free, free >= 0)


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
