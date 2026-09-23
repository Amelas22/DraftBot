"""
Tix wallet slash commands — the player-facing face of the MTGO escrow/wallet system.

Player commands (all under /wallet):
- /wallet show            your own balance + recent activity; no target to pass
- /wallet deposit <n>     hand tix to the custodian (an MTGO trade) -> wallet +n
- /wallet withdraw <n>    take tix out of the custodian (an MTGO trade) -> wallet -n
- /wallet pay @player <n> send tix to another player's wallet (internal, no trade)

Admin commands live in their own group, as /debt-admin does:
- /wallet-admin show @p   read another player's wallet; logged
- /wallet-admin reconcile audit: physical vault tix == SUM of all wallets

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
from services.mtgo_tradebot_client import EVENT_TICKET, max_cards_per_trade
from services.tournament_formatter import refresh_boards
from helpers.money_gate import (
    DEFAULT_WAIT_MINUTES, custodian_name, explain_trade_failure, gate_read, gate_serve,
    linked_username, mtgo_job_footer, mtgo_trade_prompt, spawn_followup,
)
from helpers.permissions import has_bot_manager_role
from wallet_history_view import wallet_embed


async def _send_wallet(ctx, target) -> None:
    """Render one player's wallet panel to the caller, privately.

    One helper rather than a builder plus two identical fetch-and-send tails, so
    the player's own view and the bot-manager lookup cannot drift apart.
    """
    embed, view = await wallet_embed(str(ctx.guild.id), str(target.id),
                                     target.display_name)
    await ctx.followup.send(embed=embed, view=view, ephemeral=True)


def _trades_note(n: int) -> str:
    """How many trades this will take, when it is more than one.

    The custodian refuses an order above its per-trade limit, so a large one
    goes as several trades run back to back. Saying so up front is the whole
    difference between a second trade request and a surprise.
    """
    parts = resolution.chunk_amounts(n, max_cards_per_trade())
    return "" if len(parts) == 1 else f" across **{len(parts)} trades**"


class WalletCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        logger.info("Wallet commands cog loaded")

    wallet = SlashCommandGroup("wallet", "Manage your MTGO tix wallet")
    # Admin actions get their own group, the way /debt-admin is separate from
    # /debts. No command in this repo sets default_member_permissions, so a
    # subcommand left in the player group stays visible in every player's picker
    # and only fails at invoke time.
    wallet_admin = SlashCommandGroup(
        "wallet-admin", "Admin commands for managing tix wallets")

    # ----- /wallet show -----
    @wallet.command(name="show", description="Show your tix wallet balance and recent activity")
    async def wallet_show(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)
        err = gate_read(ctx)
        if err:
            return await ctx.followup.send(err, ephemeral=True)

        await _send_wallet(ctx, ctx.author)

    # ----- /wallet-admin show <player> -----
    @wallet_admin.command(
        name="show",
        description="[Admin] Show another player's tix wallet balance and activity")
    @has_bot_manager_role()
    @option("player", discord.Member, description="Whose wallet to show")
    async def wallet_admin_show(self, ctx: discord.ApplicationContext,
                                player: discord.Member):
        await ctx.defer(ephemeral=True)
        err = gate_read(ctx)
        if err:
            return await ctx.followup.send(err, ephemeral=True)

        # A privileged read of someone else's money, recorded so "who looked at
        # my wallet" has an answer. Deliberately not a DM to the player: a
        # manager opening a support ticket is routine, and a notification every
        # time would read as an accusation.
        logger.info(f"wallet admin_show: {ctx.author.id} viewed "
                    f"{player.id} in guild {ctx.guild.id}")
        await _send_wallet(ctx, player)

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

        guild_id = str(ctx.guild.id)
        player_id = str(ctx.author.id)
        custodian = await custodian_name()
        await ctx.followup.send(
            f"**Deposit started** — **{amount} {EVENT_TICKET}(s)**{_trades_note(amount)}. "
            f"{mtgo_trade_prompt(custodian)}", ephemeral=True)

        # capture only what the poller needs (not ctx) — this task can live for ~14 min
        followup = ctx.followup
        bot = self.bot

        async def _finish():
            res = await resolution.run_deposit_order(
                guild_id, player_id, username, amount,
                commit=True, wait_minutes=DEFAULT_WAIT_MINUTES)
            credited = res["credited"]
            if credited:
                # Entry before debts, and never raising past this point: both rules
                # live in settle_deposit_inflow, which the watchdog's late-job path
                # uses too. A raise here would abort _finish before the followup
                # below, leaving a player whose deposit landed with no confirmation.
                completed, drawn = await resolution.settle_deposit_inflow(
                    guild_id, player_id)
                bal = await wallet_service.get_balance(guild_id, player_id)
                # Completed entries AND ones this captain is still short on — see
                # escrow.open_boards_for_captain.
                await refresh_boards(
                    bot, set(completed) | set(await escrow.open_boards_for_captain(player_id)))
                msg = f"✅ Deposit confirmed: **+{credited} tix**. Balance: **{bal} tix**."
                if credited < amount:
                    # Chunks credit as they land, so a run that stopped part-way
                    # leaves the player paid for what completed and owing nothing
                    # for the rest. Asking again is the whole recovery.
                    msg += (f"\n⚠️ Only **{credited}** of **{amount}** went through: "
                            f"{explain_trade_failure(res.get('error'))} "
                            f"Run the command again for the remaining **{amount - credited}**.")
                if completed:
                    msg += f" Completed **{len(completed)}** pending tournament registration(s)."
                if drawn:
                    total = sum(d.get("amount", 0) for d in drawn)
                    msg += f" Auto-applied **{total} tix** to {len(drawn)} debt(s)."
            elif res.get("busy"):
                msg = f"⏳ {res.get('error')}"
            else:
                msg = f"❌ Deposit failed: {explain_trade_failure(res.get('error'))}"
            msg += mtgo_job_footer(", ".join(res["jobs"])) if res["jobs"] else ""
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
        custodian = await custodian_name()
        await ctx.followup.send(
            f"**Withdraw started** — **{amount} tix**{_trades_note(amount)}. "
            f"{mtgo_trade_prompt(custodian)}", ephemeral=True)

        followup = ctx.followup

        async def _finish():
            res = await resolution.run_withdraw_order(
                guild_id, player_id, username, amount,
                commit=True, wait_minutes=DEFAULT_WAIT_MINUTES)
            delivered = res["delivered"]
            bal = await wallet_service.get_balance(guild_id, player_id)
            if delivered == amount:
                msg = f"✅ Withdraw confirmed: **−{delivered} tix**. Balance: **{bal} tix**."
            elif delivered:
                # Each trade commits only its own tix, so whatever did not go
                # out was never taken from the wallet. Nothing to unwind.
                msg = (f"⚠️ Only **{delivered}** of **{amount}** tix went out: "
                       f"{explain_trade_failure(res.get('error'))}\n"
                       f"The other **{amount - delivered}** are still in your wallet "
                       f"— run the command again for them. Balance: **{bal} tix**.")
            elif res.get("busy"):
                msg = f"⏳ {res.get('error')}"
            else:
                msg = (f"❌ Withdraw failed: {explain_trade_failure(res.get('error'))}\n"
                       f"Your {amount} tix are still in your wallet. Balance: **{bal} tix**.")
            msg += mtgo_job_footer(", ".join(res["jobs"])) if res["jobs"] else ""
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
            if res.get("code") == wallet_service.INSUFFICIENT_FUNDS:
                have = res.get("available", 0)
                # Says where an arriving deposit goes rather than telling the player to
                # do arithmetic about it: settle_deposit_inflow spends it on a pending
                # tournament entry first and debts second, so someone with either would
                # otherwise be surprised to find their deposit gone.
                return await ctx.followup.send(
                    f"You have **{have} tix** — sending **{amount}** needs "
                    f"**{amount - have}** more.\n"
                    f"Use `/wallet deposit` to trade tix in from MTGO. Deposits "
                    f"automatically cover any pending tournament entry first, then any "
                    f"outstanding debts — `/debts summary` shows what you owe.",
                    ephemeral=True)
            return await ctx.followup.send(f"Couldn't send tix: {res.get('error')}", ephemeral=True)

        payer_bal = await wallet_service.get_balance(guild_id, str(ctx.author.id))
        await ctx.followup.send(
            f"Sent **{amount} tix** to <@{player.id}>. Your balance: **{payer_bal} tix**.",
            ephemeral=True)

    # ----- /wallet reconcile (admin) -----
    @wallet_admin.command(
        name="reconcile", description="[Admin] Audit: vault tix vs. total of all wallets")
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
