"""
Tix wallet slash commands — the player-facing face of the MTGO escrow/wallet system.

Commands (all under /wallet):
- /wallet [player]        show a wallet: balance + recent activity
- /wallet deposit <n>     hand tix to the custodian (an MTGO trade) -> wallet +n
- /wallet withdraw <n>    take tix out of the custodian (an MTGO trade) -> wallet -n
- /wallet pay @player <n> send tix to another player's wallet (internal, no trade)
- /wallet reconcile       (admin) audit: physical vault tix == SUM of all wallets

Gating: enabled only on money servers with the TradeBot integration configured
(MTGO_TRADEBOT_URL + _TOKEN). Deposits/withdraws require the caller to have linked their
MTGO account (`/link_mtgo`); pay requires both parties linked so the tix stay usable.

The serve runs deposits/withdraws as async jobs. A command enqueues, replies immediately
with in-client instructions, then a background task polls the job to a terminal state and
posts the outcome — the ledger is only ever written on a completed trade. The serve's own
--commit arm state remains the master safety switch for whether a trade actually fires.
"""
import discord
from discord.ext import commands
from discord.commands import SlashCommandGroup, option
from loguru import logger

from models.mtgo_account import MtgoAccount
from services import wallet_service
from services import mtgo_resolution_service as resolution
from services import tournament_escrow_service as escrow
from services.mtgo_tradebot_client import EVENT_TICKET
from helpers.money_gate import (
    DEFAULT_WAIT_MINUTES, custodian_name, gate_read, gate_serve, linked_username,
    serve_busy_reason, spawn_followup,
)
from helpers.permissions import has_bot_manager_role


class WalletCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        logger.info("Wallet commands cog loaded")

    wallet = SlashCommandGroup("wallet", "Manage your MTGO tix wallet")

    # ----- /wallet [player] -----
    @wallet.command(name="show", description="Show a tix wallet balance and recent activity")
    @option("player", discord.Member, description="Whose wallet to show (defaults to you)", required=False)
    async def wallet_show(self, ctx: discord.ApplicationContext, player: discord.Member = None):
        await ctx.defer(ephemeral=True)
        err = gate_read(ctx)
        if err:
            return await ctx.followup.send(err, ephemeral=True)

        guild_id = str(ctx.guild.id)
        target = player or ctx.author
        w = await wallet_service.get_wallet(guild_id, str(target.id))
        history = await wallet_service.get_history(guild_id, str(target.id), limit=10)

        embed = discord.Embed(
            title=f"{target.display_name}'s Tix Wallet", color=discord.Color.gold())
        embed.add_field(name="Balance", value=f"**{w.balance}** tix", inline=True)

        if history:
            lines = []
            for tx in history:
                sign = "+" if tx.amount >= 0 else "−"
                # counterparty is a Discord id (all-digits) for player transfers; an MTGO
                # username or a synthetic holder otherwise — only mention the former.
                who = f" ↔ <@{tx.counterparty_id}>" if tx.counterparty_id and tx.counterparty_id.isdigit() else ""
                lines.append(f"`{sign}{abs(tx.amount)}` {tx.kind}{who}")
            embed.add_field(name="Recent activity", value="\n".join(lines), inline=False)
        else:
            embed.set_footer(text="No wallet activity yet.")

        await ctx.followup.send(embed=embed, ephemeral=True)

    # ----- /wallet deposit <n> -----
    @wallet.command(name="deposit", description="Deposit tix into your wallet (trade them to the custodian)")
    @option("amount", int, description="How many tix to deposit", min_value=1)
    async def wallet_deposit(self, ctx: discord.ApplicationContext, amount: int):
        await ctx.defer(ephemeral=True)
        err = gate_serve(ctx)
        if err:
            return await ctx.followup.send(err, ephemeral=True)
        username = await linked_username(ctx.author.id)
        if not username:
            return await ctx.followup.send(
                "Link your MTGO account first with `/link_mtgo <username>`.", ephemeral=True)

        # One trade at a time: say so rather than queue behind someone else's job.
        busy = await serve_busy_reason()
        if busy:
            return await ctx.followup.send(f"⏳ {busy}", ephemeral=True)

        guild_id = str(ctx.guild.id)
        player_id = str(ctx.author.id)
        started = await resolution.start_deposit(
            guild_id, player_id, username, amount, commit=True, wait_minutes=DEFAULT_WAIT_MINUTES)
        if not started.get("ok"):
            return await ctx.followup.send(
                f"Couldn't start the deposit: {started.get('error')}", ephemeral=True)

        job_id = started["job_id"]
        custodian = await custodian_name()
        await ctx.followup.send(
            f"**Deposit started.** In MTGO, trade **{amount} {EVENT_TICKET}(s)** to "
            f"`{custodian}` and accept when the trade window pops. I'll confirm here once it "
            f"lands.\n_Job `{job_id}` — you have ~{DEFAULT_WAIT_MINUTES} min._", ephemeral=True)

        # capture only what the poller needs (not ctx) — this task can live for ~14 min
        followup = ctx.followup

        async def _finish():
            res = await resolution.finish_deposit(job_id, guild_id, player_id, amount, username)
            if res.get("ok"):
                # Complete any pending tournament entry BEFORE auto-draw: the player
                # just committed to that entry, so its fee shouldn't be siphoned to a
                # creditor on the way in.
                entries = await escrow.sweep_pending_entries()
                bal = await wallet_service.get_balance(guild_id, player_id)
                drawn = await resolution.auto_draw(guild_id, player_id)
                msg = f"✅ Deposit confirmed: **+{amount} tix**. Balance: **{bal} tix**."
                if entries:
                    msg += f" Completed **{entries}** pending tournament registration(s)."
                if drawn:
                    total = sum(d.get("amount", 0) for d in drawn)
                    msg += f" Auto-applied **{total} tix** to {len(drawn)} debt(s)."
            elif res.get("outcome") == "pending":
                msg = (f"⏳ Deposit `{job_id}` is still pending — it'll credit automatically "
                       f"once the trade completes.")
            else:
                msg = f"❌ Deposit `{job_id}` failed: {res.get('error')}"
            await followup.send(msg, ephemeral=True)

        spawn_followup("wallet deposit", _finish())

    # ----- /wallet withdraw <n> -----
    @wallet.command(name="withdraw", description="Withdraw tix from your wallet (the custodian trades them to you)")
    @option("amount", int, description="How many tix to withdraw", min_value=1)
    async def wallet_withdraw(self, ctx: discord.ApplicationContext, amount: int):
        await ctx.defer(ephemeral=True)
        err = gate_serve(ctx)
        if err:
            return await ctx.followup.send(err, ephemeral=True)
        username = await linked_username(ctx.author.id)
        if not username:
            return await ctx.followup.send(
                "Link your MTGO account first with `/link_mtgo <username>`.", ephemeral=True)

        guild_id = str(ctx.guild.id)
        player_id = str(ctx.author.id)
        started = await resolution.start_withdraw(
            guild_id, player_id, username, amount, commit=True, wait_minutes=DEFAULT_WAIT_MINUTES)
        if not started.get("ok"):
            # covers insufficient funds and a serve that wouldn't accept the job
            return await ctx.followup.send(
                f"Couldn't start the withdraw: {started.get('error')}", ephemeral=True)

        job_id = started["job_id"]
        in_flight_source = started["in_flight_source"]
        custodian = await custodian_name()
        await ctx.followup.send(
            f"**Withdraw started** — {amount} tix committed. In MTGO, accept the trade from "
            f"`{custodian}` when it pops. I'll confirm here once it completes.\n"
            f"_Job `{job_id}` — you have ~{DEFAULT_WAIT_MINUTES} min._", ephemeral=True)

        followup = ctx.followup

        async def _finish():
            res = await resolution.finish_withdraw(
                in_flight_source, job_id, guild_id, player_id, amount, username)
            if res.get("ok"):
                bal = await wallet_service.get_balance(guild_id, player_id)
                msg = f"✅ Withdraw confirmed: **−{amount} tix**. Balance: **{bal} tix**."
            elif res.get("outcome") == "pending":
                msg = (f"⏳ Withdraw `{job_id}` is still running; your {amount} tix stay "
                       f"committed to it until it resolves.")
            else:
                msg = (f"❌ Withdraw `{job_id}` failed: {res.get('error')}. "
                       f"Your {amount} tix have been released.")
            await followup.send(msg, ephemeral=True)

        spawn_followup("wallet withdraw", _finish())

    # ----- /wallet pay @player <n> -----
    @wallet.command(name="pay", description="Send tix from your wallet to another player (no MTGO trade)")
    @option("player", discord.Member, description="Who to pay")
    @option("amount", int, description="How many tix to send", min_value=1)
    async def wallet_pay(self, ctx: discord.ApplicationContext, player: discord.Member, amount: int):
        await ctx.defer(ephemeral=True)
        err = gate_read(ctx)
        if err:
            return await ctx.followup.send(err, ephemeral=True)
        if player.id == ctx.author.id:
            return await ctx.followup.send("You can't pay yourself.", ephemeral=True)
        # both parties linked so the recipient can actually use the tix later (one batch query)
        linked = await MtgoAccount.usernames_for_discord_ids([ctx.author.id, player.id])
        if str(ctx.author.id) not in linked:
            return await ctx.followup.send(
                "Link your MTGO account first with `/link_mtgo`.", ephemeral=True)
        if str(player.id) not in linked:
            return await ctx.followup.send(
                f"{player.display_name} hasn't linked an MTGO account yet, so they can't "
                f"receive tix. Ask them to run `/link_mtgo` first.", ephemeral=True)

        guild_id = str(ctx.guild.id)
        res = await resolution.pay(
            guild_id, str(ctx.author.id), str(player.id), amount,
            notes=f"pay to {player.display_name}")
        if not res.get("ok"):
            return await ctx.followup.send(f"Couldn't send tix: {res.get('error')}", ephemeral=True)

        payer_bal = await wallet_service.get_balance(guild_id, str(ctx.author.id))
        await ctx.followup.send(
            f"Sent **{amount} tix** to <@{player.id}>. Your balance: **{payer_bal} tix**.",
            ephemeral=True)

    # ----- /wallet reconcile (admin) -----
    @wallet.command(name="reconcile", description="(Admin) Audit: vault tix vs. total of all wallets")
    @has_bot_manager_role()
    async def wallet_reconcile(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)
        err = gate_serve(ctx)
        if err:
            return await ctx.followup.send(err, ephemeral=True)

        res = await resolution.reconcile()  # global: one vault across guilds
        if res.get("error"):
            return await ctx.followup.send(f"Couldn't reconcile: {res['error']}", ephemeral=True)

        color = discord.Color.green() if res["ok"] else discord.Color.red()
        embed = discord.Embed(title="Wallet Reconciliation", color=color)
        embed.add_field(name="Vault tix (physical)", value=str(res["bot_tix"]), inline=True)
        embed.add_field(name="Wallets total (claims)", value=str(res["wallet_total"]), inline=True)
        embed.add_field(name="Difference", value=f"{res['diff']:+d}", inline=True)
        embed.set_footer(text="✅ Balanced" if res["ok"]
                         else "⚠️ MISMATCH — the vault and the ledger disagree.")
        await ctx.followup.send(embed=embed, ephemeral=True)


def setup(bot):
    bot.add_cog(WalletCommands(bot))
