"""
Debt tracking slash commands.

Commands:
- /debts - View your own debt summary
- /debts @player - View debts with a specific player
- /debts history @player - View audit history with a player
- /settle @player - Settle debts with a player
- /debts-admin - Admin view of all guild debts
"""
import discord
from discord.ext import commands
from discord.commands import SlashCommandGroup, option
from loguru import logger

from services.debt_service import (
    get_all_balances_for,
    get_balance_with,
    get_entries_since_last_settlement,
    create_settlement
)
from debt_views.settle_views import (
    CounterpartySelectView,
    AmountInputView
)
from debt_views.helpers import get_member_name, format_entry_source
from database.db_session import db_session
from models.debt_ledger import DebtLedger
from sqlalchemy import select, func


class DebtCommands(commands.Cog):
    """Cog for debt tracking slash commands."""

    def __init__(self, bot):
        self.bot = bot
        logger.info("Debt commands cog loaded")

    debts = SlashCommandGroup("debts", "View and manage your debts")

    @debts.command(name="summary", description="View your debt summary")
    async def debts_summary(self, ctx: discord.ApplicationContext):
        """View your own debt summary with all counterparties."""
        await ctx.defer(ephemeral=True)

        user_id = str(ctx.author.id)
        guild_id = str(ctx.guild.id)

        balances = await get_all_balances_for(
            guild_id=guild_id,
            player_id=user_id
        )

        if not balances:
            await ctx.followup.send("You have no outstanding debts with anyone.")
            return

        embed = discord.Embed(
            title="Your Debt Summary",
            color=discord.Color.gold()
        )

        you_owe_lines = []
        owed_to_you_lines = []
        total_owe = 0
        total_owed = 0

        for counterparty_id, balance in balances.items():
            name = get_member_name(ctx.guild, counterparty_id)

            # Get entry count since last settlement
            entries = await get_entries_since_last_settlement(
                guild_id=guild_id,
                player_id=user_id,
                counterparty_id=counterparty_id
            )
            entry_count = len([e for e in entries if e.source_type == 'draft'])

            if balance < 0:
                # User owes them
                you_owe_lines.append(f"<@{counterparty_id}>: {abs(balance)} tix ({entry_count} drafts)")
                total_owe += abs(balance)
            else:
                # They owe user
                owed_to_you_lines.append(f"<@{counterparty_id}>: {balance} tix ({entry_count} drafts)")
                total_owed += balance

        if you_owe_lines:
            embed.add_field(
                name=f"You Owe (Total: {total_owe} tix)",
                value="\n".join(you_owe_lines),
                inline=False
            )

        if owed_to_you_lines:
            embed.add_field(
                name=f"Owed to You (Total: {total_owed} tix)",
                value="\n".join(owed_to_you_lines),
                inline=False
            )

        net = total_owed - total_owe
        if net > 0:
            embed.set_footer(text=f"Net position: You are owed {net} tix overall")
        elif net < 0:
            embed.set_footer(text=f"Net position: You owe {abs(net)} tix overall")
        else:
            embed.set_footer(text="Net position: Even")

        await ctx.followup.send(embed=embed)

    @debts.command(name="with", description="View debts with a specific player")
    @option("player", discord.Member, description="The player to check debts with")
    async def debts_with(self, ctx: discord.ApplicationContext, player: discord.Member):
        """View debt details with a specific player."""
        await ctx.defer(ephemeral=True)

        user_id = str(ctx.author.id)
        counterparty_id = str(player.id)
        guild_id = str(ctx.guild.id)

        if user_id == counterparty_id:
            await ctx.followup.send("You can't have debts with yourself!")
            return

        balance = await get_balance_with(
            guild_id=guild_id,
            player_id=user_id,
            counterparty_id=counterparty_id
        )

        entries = await get_entries_since_last_settlement(
            guild_id=guild_id,
            player_id=user_id,
            counterparty_id=counterparty_id
        )

        embed = discord.Embed(
            title=f"Debts with {player.display_name}",
            color=discord.Color.blue()
        )

        # Net balance
        if balance == 0:
            balance_text = "No outstanding debt"
        elif balance < 0:
            balance_text = f"You owe **{abs(balance)} tix**"
        else:
            balance_text = f"They owe you **{balance} tix**"

        embed.add_field(
            name="Net Balance",
            value=balance_text,
            inline=False
        )

        # Breakdown since last settlement
        if entries:
            breakdown_lines = []
            for entry in entries[-15:]:  # Last 15 entries
                source = format_entry_source(entry)
                date_str = entry.created_at.strftime("%b %d") if entry.created_at else ""

                if entry.amount < 0:
                    breakdown_lines.append(f"{source} ({date_str}): You owe {abs(entry.amount)} tix")
                else:
                    breakdown_lines.append(f"{source} ({date_str}): They owe you {entry.amount} tix")

            if len(entries) > 15:
                breakdown_lines.append(f"... and {len(entries) - 15} more")

            embed.add_field(
                name="Breakdown (since last settlement)",
                value="\n".join(breakdown_lines) or "No entries",
                inline=False
            )
        else:
            embed.add_field(
                name="Breakdown",
                value="No debt entries found",
                inline=False
            )

        embed.set_footer(text=f"Use /settle @{player.display_name} to settle")

        await ctx.followup.send(embed=embed)

    @debts.command(name="history", description="View full debt history with a player")
    @option("player", discord.Member, description="The player to check history with")
    async def debts_history(self, ctx: discord.ApplicationContext, player: discord.Member):
        """View full audit history with a specific player."""
        await ctx.defer(ephemeral=True)

        user_id = str(ctx.author.id)
        counterparty_id = str(player.id)
        guild_id = str(ctx.guild.id)

        if user_id == counterparty_id:
            await ctx.followup.send("You can't have debts with yourself!")
            return

        # Get ALL entries (not just since last settlement)
        async with db_session() as session:
            query = (
                select(DebtLedger)
                .where(
                    DebtLedger.guild_id == guild_id,
                    DebtLedger.player_id == user_id,
                    DebtLedger.counterparty_id == counterparty_id
                )
                .order_by(DebtLedger.created_at.desc())
                .limit(50)
            )
            result = await session.execute(query)
            entries = result.scalars().all()

        embed = discord.Embed(
            title=f"Debt History with {player.display_name}",
            color=discord.Color.purple()
        )

        if entries:
            history_lines = []
            for entry in entries:
                date_str = entry.created_at.strftime("%b %d") if entry.created_at else "?"
                source = format_entry_source(entry)

                if entry.amount < 0:
                    history_lines.append(f"{date_str}: {source} - You owe {abs(entry.amount)} tix")
                else:
                    history_lines.append(f"{date_str}: {source} - They owe you {entry.amount} tix")

            # Split into multiple fields if needed (Discord limit)
            chunk_size = 15
            for i in range(0, len(history_lines), chunk_size):
                chunk = history_lines[i:i+chunk_size]
                field_name = "History" if i == 0 else "History (cont.)"
                embed.add_field(
                    name=field_name,
                    value="\n".join(chunk),
                    inline=False
                )
        else:
            embed.add_field(
                name="History",
                value="No debt history found",
                inline=False
            )

        await ctx.followup.send(embed=embed)

    @discord.slash_command(name="settle", description="Settle debts with a player")
    @option("player", discord.Member, description="The player to settle with")
    async def settle(self, ctx: discord.ApplicationContext, player: discord.Member):
        """Settle debts with a specific player."""
        await ctx.defer(ephemeral=True)

        user_id = str(ctx.author.id)
        counterparty_id = str(player.id)
        guild_id = str(ctx.guild.id)

        if user_id == counterparty_id:
            await ctx.followup.send("You can't settle debts with yourself!")
            return

        balance = await get_balance_with(
            guild_id=guild_id,
            player_id=user_id,
            counterparty_id=counterparty_id
        )

        if balance == 0:
            await ctx.followup.send(f"You have no outstanding debts with {player.display_name}.")
            return

        # Get breakdown
        entries = await get_entries_since_last_settlement(
            guild_id=guild_id,
            player_id=user_id,
            counterparty_id=counterparty_id
        )

        embed = discord.Embed(
            title=f"Settle with {player.display_name}",
            color=discord.Color.blue()
        )

        # Show net balance
        if balance < 0:
            embed.add_field(
                name="Net Balance",
                value=f"You owe **{abs(balance)} tix**",
                inline=False
            )
        else:
            embed.add_field(
                name="Net Balance",
                value=f"They owe you **{balance} tix**",
                inline=False
            )

        # Show breakdown
        if entries:
            breakdown_lines = []
            for entry in entries[-10:]:
                source = format_entry_source(entry)

                if entry.amount < 0:
                    breakdown_lines.append(f"{source}: You owe {abs(entry.amount)} tix")
                else:
                    breakdown_lines.append(f"{source}: They owe you {entry.amount} tix")

            if len(entries) > 10:
                breakdown_lines.append(f"... and {len(entries) - 10} more")

            embed.add_field(
                name="Breakdown (since last settlement)",
                value="\n".join(breakdown_lines),
                inline=False
            )

        embed.set_footer(text="Click 'Enter Amount' to confirm the payment amount")

        view = AmountInputView(
            user_id=user_id,
            guild_id=guild_id,
            counterparty_id=counterparty_id,
            net_balance=balance,
            counterparty_name=player.display_name
        )

        await ctx.followup.send(embed=embed, view=view)

    @discord.slash_command(name="debts-admin", description="[Admin] View all debts in the guild")
    @commands.has_permissions(administrator=True)
    async def debts_admin(self, ctx: discord.ApplicationContext):
        """Admin view of all outstanding debts in the guild."""
        await ctx.defer(ephemeral=True)

        guild_id = str(ctx.guild.id)

        # Get all non-zero balances in the guild
        async with db_session() as session:
            # Get unique player-counterparty pairs with non-zero balance
            query = (
                select(
                    DebtLedger.player_id,
                    DebtLedger.counterparty_id,
                    func.sum(DebtLedger.amount).label('balance')
                )
                .where(DebtLedger.guild_id == guild_id)
                .group_by(DebtLedger.player_id, DebtLedger.counterparty_id)
                .having(func.sum(DebtLedger.amount) < 0)  # Only show debts (negative balances)
                .order_by(func.sum(DebtLedger.amount).asc())  # Biggest debts first
            )
            result = await session.execute(query)
            rows = result.all()

        if not rows:
            await ctx.followup.send("No outstanding debts in this guild.")
            return

        embed = discord.Embed(
            title="Guild Debt Summary",
            description="All outstanding debts (showing debtor perspective)",
            color=discord.Color.orange()
        )

        debt_lines = []
        total = 0
        for row in rows[:25]:  # Limit to 25 entries
            debtor_name = get_member_name(ctx.guild, row.player_id)
            creditor_name = get_member_name(ctx.guild, row.counterparty_id)

            amount = abs(row.balance)
            debt_lines.append(f"{debtor_name} owes {creditor_name}: {amount} tix")
            total += amount

        embed.add_field(
            name=f"Outstanding Debts (Total: {total} tix)",
            value="\n".join(debt_lines) if debt_lines else "None",
            inline=False
        )

        if len(rows) > 25:
            embed.set_footer(text=f"Showing 25 of {len(rows)} debt relationships")

        await ctx.followup.send(embed=embed)


def setup(bot):
    bot.add_cog(DebtCommands(bot))
