"""
Shared gating/identity helpers for the money stack (wallet + tournament escrow cogs).

One home for the checks every money-touching command runs, so the rules and their
user-facing wording can't drift between cogs:
  * gate_read  — the guild must be money-enabled (wallet views, internal pays).
  * gate_serve — read gate + the TradeBot serve must be configured (trades).
  * linked_username — the caller's /link_mtgo identity, required to trade.
  * custodian_name — the vault account's display name (cached; it only changes
    when the serve is re-attached to a different MTGO login, i.e. on a restart
    of the serve — and a bot restart clears the cache).

Also the shared background-followup spawner: deposit/withdraw/escrow commands
reply immediately and poll the MTGO job in a fire-and-forget task; spawn_followup
gives that task the never-crash-silently wrapper (and keeps a strong reference so
it can't be garbage-collected mid-poll).
"""
import asyncio

from loguru import logger

from config import is_money_server
from models.mtgo_account import MtgoAccount
from services.mtgo_tradebot_client import get_client

# Serve-side readiness wait (minutes) for a deposit/withdraw trade before the job
# fails and any reservation is released. Bounds how long a player has to accept
# the in-client trade.
DEFAULT_WAIT_MINUTES = 10

# Entry-fee deposits get a longer window: the custodian works one trade at a time,
# so a captain registering while the bot is mid-trade may wait out someone else's
# job before theirs even starts. Observed live: a busy serve took ~28 minutes to
# work a queued deposit. The registration itself never expires on this — a pending
# team stays registrable until the tournament starts (see sweep_pending_entries) —
# this only sets how long the bot keeps one trade window open.
ESCROW_WAIT_MINUTES = 45

_custodian_cache: str | None = None
_background_tasks: set = set()


def gate_read(ctx) -> str | None:
    """Wallet must be used in a money server. Returns an error string, or None."""
    if not ctx.guild:
        return "Wallet commands can only be used in a server."
    if not is_money_server(str(ctx.guild.id)):
        return "The tix wallet is only available on money-enabled servers."
    return None


def gate_serve(ctx) -> str | None:
    """Read gate + the TradeBot integration must be configured."""
    err = gate_read(ctx)
    if err:
        return err
    if not get_client().enabled:
        return ("The MTGO TradeBot integration isn't configured on this bot "
                "(set MTGO_TRADEBOT_URL and MTGO_TRADEBOT_TOKEN).")
    return None


async def linked_username(discord_id) -> str | None:
    acct = await MtgoAccount.get_for_discord(discord_id)
    return acct.mtgo_username if acct else None


async def custodian_name() -> str:
    """The vault account's display name, from the serve's /health (cached on success)."""
    global _custodian_cache
    if _custodian_cache:
        return _custodian_cache
    health = await get_client().health()
    name = (health or {}).get("custodian")
    if isinstance(name, str) and name:
        _custodian_cache = name
        return name
    return "the custodian bot"


def spawn_followup(label: str, coro) -> asyncio.Task:
    """Run a background follow-up coroutine that logs (never raises) on failure."""
    async def _guarded():
        try:
            await coro
        except Exception as e:
            logger.warning(f"{label} follow-up failed: {e}")

    task = asyncio.create_task(_guarded())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task
