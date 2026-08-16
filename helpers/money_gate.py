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

# Serve-side readiness wait (minutes) before the job fails. Bounds how long a player
# has to accept the in-client trade.
DEFAULT_WAIT_MINUTES = 10

_custodian_cache: str | None = None
_background_tasks: set = set()


WALLET_HOWTO_TITLE = "💡 Paying with tix"


def wallet_howto(guild_id, *, brief=False) -> str | None:
    """How to pay with tix, in the one place the wording lives.

    It appears wherever a player is confronted with tix they owe — the bet outcomes
    of a finished staked draft, their own balances, the guild debt summary — and once
    before they bet at all. Written once here because four copies drift, and because
    the middle line is the part nobody guesses: funding a wallet settles debts by
    itself, on any inflow, not just a deposit.

    Returns None where there is no wallet to use. Staked drafts only run on money
    servers, but debts do not: a card loan via /lend books one on a free server too,
    so the debt panels render there. Pointing those players at a command the bot
    refuses is worse than saying nothing, and this is the check they'd each otherwise
    have to remember.
    """
    if not is_money_server(str(guild_id)):
        return None
    if brief:
        return ("Bets settle from your tix wallet — `/wallet deposit <n>` covers "
                "what you owe automatically.")
    return (
        "`/wallet deposit <n>` — credits your wallet, and pays your oldest debts automatically\n"
        "`/wallet show` — your balance and recent activity\n"
        "`/wallet pay @player <n>` — send tix straight to someone"
    )


def add_wallet_howto(embed, guild_id) -> bool:
    """Append the how-to to an embed, where there is a wallet to explain.

    The three embed call sites otherwise each repeat fetch-check-add and import both
    the text and its title; this leaves them one line and one import. Returns whether
    anything was added, for callers that care.
    """
    text = wallet_howto(guild_id)
    if not text:
        return False
    embed.add_field(name=WALLET_HOWTO_TITLE, value=text, inline=False)
    return True


def mtgo_trade_prompt(custodian: str) -> str:
    """How a player completes an MTGO trade with the custodian.

    Deposits and withdrawals drive the same serve and so take the same steps, but the
    two commands used to describe them differently — deposit told the player to open
    the trade themselves, which is not how the serve works at all. One string so the
    instructions cannot disagree about a protocol that is identical.
    """
    return (f"`{custodian}` will message you in MTGO — reply **YES** and it will open "
            f"the trade. Accept it and I'll confirm here once it lands.")


def mtgo_job_footer(job_id: str) -> str:
    """The job reference and how long the player has to accept.

    Folded in beside the prompt for the same reason the prompt itself exists: the two
    commands rendered this line identically and separately, which is exactly how the
    instructions above it drifted apart in the first place.
    """
    return f"\n_Job `{job_id}` — you have ~{DEFAULT_WAIT_MINUTES} min._"


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


async def serve_busy_reason() -> str | None:
    """Why the custodian can't take a trade right now, or None if it's free.

    The custodian works ONE trade at a time, so enqueuing behind an in-flight job just
    leaves the player staring at a trade window that won't open for minutes. Better to
    say so and let them come back.

    Busy-ness comes from the job LIST (states queued/running), never from /health's
    ``jobs`` field: that counter includes terminal jobs, so a single past failure would
    otherwise wedge every future deposit behind a permanent "busy".
    """
    global _custodian_cache
    client = get_client()
    health = await client.health()
    if health and not _custodian_cache and isinstance(health.get("custodian"), str):
        _custodian_cache = health["custodian"]  # saves custodian_name() its own /health
    if not health or not health.get("ok"):
        return ("The MTGO custodian isn't reachable right now. Try again in a few "
                "minutes — nothing has been charged.")
    if health.get("reconnecting"):
        return ("The MTGO custodian is reconnecting to the client. Try again in a few "
                "minutes.")
    active = await client.active_jobs()
    if active:
        return (f"The MTGO custodian is busy with {len(active)} other trade(s) — it can "
                f"only trade with one person at a time. Try again in a few minutes.")
    return None


def explain_trade_failure(detail: str) -> str:
    """Add an actionable hint to a serve failure the player can fix themselves.

    The serve reports raw causes ("could not resolve 'basic3' as an MTGO user"); on its
    own that reads like a bot fault, when in fact the linked username is wrong or the
    tix were never put in the trade window."""
    text = (detail or "trade failed").strip()
    low = text.lower()
    if "resolve" in low and "mtgo user" in low:
        return (f"{text}\nThat's the MTGO username linked to your Discord account — check "
                f"it with `/mtgo_whoami` and fix it with `/link_mtgo <username>` "
                f"(spelling must match your MTGO login exactly), then try again.")
    if "not presented" in low or "cards not presented" in low:
        return (f"{text}\nThe trade window opened but the tix weren't added to it. Accept "
                f"the trade, put the tix in, and confirm — then try again.")
    return text


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
