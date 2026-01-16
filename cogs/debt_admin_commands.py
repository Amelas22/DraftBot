"""
Admin debt management slash commands.

Commands:
- /debt-admin adjust - Manually adjust debt between two players (create, modify, forgive)
- /debt-admin stats - View comprehensive guild debt statistics
- /debt-admin history - View audit trail of admin debt modifications
"""
import discord
from discord.ext import commands
from discord.commands import SlashCommandGroup, option
from loguru import logger

from services.debt_service import (
    adjust_debt,
    get_balance_with,
    get_guild_debt_stats,
    get_debt_history
)
from debt_views.helpers import get_member_name
from helpers.permissions import has_bot_manager_role
from utils import update_debt_summary_for_guild


class DebtAdminCommands(commands.Cog):
    """Cog for admin debt management slash commands."""

    def __init__(self, bot):
        self.bot = bot
        logger.info("Debt admin commands cog loaded")

    debt_admin = SlashCommandGroup(
        "debt-admin",
        "Admin commands for managing debts"
    )

    @debt_admin.command(name="adjust", description="[Admin] Adjust debt between two players")
    @option("player1", discord.User, description="First player")
    @option("player2", discord.User, description="Second player")
    @option("amount", int, description="Amount to adjust (positive = player1 owes more, negative = less)")
    @option("notes", str, description="Reason for this adjustment")
    @has_bot_manager_role()
    async def debt_admin_adjust(
        self,
        ctx: discord.ApplicationContext,
        player1: discord.User,
        player2: discord.User,
        amount: int,
        notes: str
    ):
        """Adjust debt between two players (create, modify, or forgive)."""
        await ctx.defer(ephemeral=True)

        try:
            # Validation
            if player1.id == player2.id:
                await ctx.followup.send("❌ Cannot adjust debt between the same player", ephemeral=True)
                return

            if amount == 0:
                await ctx.followup.send("❌ Amount cannot be zero", ephemeral=True)
                return

            guild_id = str(ctx.guild.id)
            player1_id = str(player1.id)
            player2_id = str(player2.id)

            # Get current balance before adjustment
            old_balance = await get_balance_with(guild_id, player1_id, player2_id)

            # Call service function to adjust debt
            new_balance = await adjust_debt(
                guild_id=guild_id,
                player1_id=player1_id,
                player2_id=player2_id,
                amount=amount,
                notes=notes,
                created_by=str(ctx.author.id)
            )

            # Update debt summary message if it exists
            await update_debt_summary_for_guild(self.bot, str(ctx.guild.id))

            # Format success message
            direction = "more" if amount > 0 else "less"
            logger.info(
                f"Admin {ctx.author.name} adjusted debt: {player1.name} <-> {player2.name} by {amount}"
            )

            # Build balance description
            if new_balance == 0:
                balance_desc = f"Debt cleared! {player1.mention} and {player2.mention} are even"
                old_balance_desc = self._format_balance(old_balance, player1, player2)
            elif new_balance < 0:
                balance_desc = f"{player1.mention} owes {player2.mention} **{abs(new_balance)} tix**"
                old_balance_desc = self._format_balance(old_balance, player1, player2)
            else:
                balance_desc = f"{player2.mention} owes {player1.mention} **{abs(new_balance)} tix**"
                old_balance_desc = self._format_balance(old_balance, player1, player2)

            await ctx.followup.send(
                f"✅ Adjusted debt between {player1.mention} and {player2.mention}\n"
                f"**Adjustment:** {amount:+d} tix ({player1.mention} owes {direction})\n"
                f"**Reason:** {notes}\n"
                f"**Previous balance:** {old_balance_desc}\n"
                f"**New balance:** {balance_desc}",
                ephemeral=True
            )

        except ValueError as e:
            logger.warning(f"Validation error in debt adjustment: {e}")
            await ctx.followup.send(f"❌ {str(e)}", ephemeral=True)
        except Exception as e:
            logger.error(f"Error adjusting debt: {e}")
            await ctx.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @debt_admin.command(name="stats", description="[Admin] View guild debt statistics")
    @option(
        "timeframe",
        str,
        description="Timeframe for statistics",
        choices=["all_time", "last_7_days", "last_30_days", "since_last_settlement"],
        default="all_time"
    )
    @has_bot_manager_role()
    async def debt_admin_stats(
        self,
        ctx: discord.ApplicationContext,
        timeframe: str = "all_time"
    ):
        """View comprehensive debt statistics for the guild."""
        await ctx.defer(ephemeral=True)

        try:
            guild_id = str(ctx.guild.id)
            stats = await get_guild_debt_stats(guild_id, timeframe)

            # Build embed
            timeframe_display = timeframe.replace("_", " ").title()
            embed = discord.Embed(
                title=f"Guild Debt Statistics",
                description=f"Timeframe: **{timeframe_display}**",
                color=discord.Color.blue()
            )

            # Total debt section
            embed.add_field(
                name="💰 Total Outstanding Debt",
                value=f"{stats['total_debt']} tix",
                inline=True
            )

            # Player counts
            embed.add_field(
                name="👥 Players",
                value=f"{stats['num_debtors']} debtors\n{stats['num_creditors']} creditors",
                inline=True
            )

            # Average debt
            avg_debt = int(stats['avg_debt_per_debtor'])
            embed.add_field(
                name="📊 Average Debt",
                value=f"{avg_debt} tix per debtor",
                inline=True
            )

            # Largest debt
            if stats['largest_debt']:
                debtor_id, creditor_id, amount = stats['largest_debt']
                debtor_name = get_member_name(ctx.guild, debtor_id)
                creditor_name = get_member_name(ctx.guild, creditor_id)
                embed.add_field(
                    name="🔝 Largest Individual Debt",
                    value=f"{debtor_name} owes {creditor_name}\n**{amount} tix**",
                    inline=False
                )

            # Most active debtor
            if stats['most_active_debtor']:
                player_id, entry_count = stats['most_active_debtor']
                player_name = get_member_name(ctx.guild, player_id)
                embed.add_field(
                    name="📈 Most Active Player",
                    value=f"{player_name}\n{entry_count} debt entries",
                    inline=True
                )

            # Recent activity
            embed.add_field(
                name="🔄 Recent Activity",
                value=f"{stats['recent_activity']} debt entries",
                inline=True
            )

            # Debt by source
            if stats['debt_by_source']:
                source_lines = []
                for source_type, count in stats['debt_by_source'].items():
                    source_lines.append(f"{source_type.title()}: {count}")
                embed.add_field(
                    name="📋 Entries by Source",
                    value="\n".join(source_lines),
                    inline=True
                )

            logger.info(f"Admin {ctx.author.name} viewed debt stats for guild {guild_id} ({timeframe})")
            await ctx.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error fetching debt stats: {e}")
            await ctx.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @debt_admin.command(name="history", description="[Admin] View debt history (all sources)")
    @option("player", discord.User, description="Filter by specific player (optional)", required=False)
    @option("limit", int, description="Number of entries to show (max 100)", default=25, min_value=1, max_value=100)
    @has_bot_manager_role()
    async def debt_admin_history(
        self,
        ctx: discord.ApplicationContext,
        player: discord.User = None,
        limit: int = 25
    ):
        """View complete debt history (drafts, settlements, and admin modifications)."""
        await ctx.defer(ephemeral=True)

        try:
            guild_id = str(ctx.guild.id)
            player_id = str(player.id) if player else None

            entries = await get_debt_history(guild_id, player_id, limit)

            if not entries:
                filter_msg = f" for {player.display_name}" if player else ""
                await ctx.followup.send(f"No debt history found{filter_msg}.", ephemeral=True)
                return

            # Build embed
            title = "Debt History"
            if player:
                title += f" - {player.display_name}"

            embed = discord.Embed(
                title=title,
                color=discord.Color.purple()
            )

            # Group entries by source_id (pairs)
            grouped_entries = {}
            for entry in entries:
                if entry.source_id not in grouped_entries:
                    grouped_entries[entry.source_id] = []
                grouped_entries[entry.source_id].append(entry)

            # Build history lines
            history_lines = []
            for source_id, group in grouped_entries.items():
                # Take the first entry as representative (they share same timestamp, notes, etc.)
                rep_entry = group[0]
                date_str = rep_entry.created_at.strftime("%b %d, %Y %I:%M %p") if rep_entry.created_at else "?"

                # Determine who owes whom
                # Find the entry with negative amount (debtor's perspective)
                debtor_entry = next((e for e in group if e.amount < 0), group[0])
                amount = abs(debtor_entry.amount)

                debtor_name = get_member_name(ctx.guild, debtor_entry.player_id)
                creditor_name = get_member_name(ctx.guild, debtor_entry.counterparty_id)

                # Format based on source type
                source_type = rep_entry.source_type.title()

                # Add source-specific info
                if rep_entry.source_type == 'draft':
                    source_info = f"Draft #{rep_entry.source_id}"
                elif rep_entry.source_type == 'settlement':
                    source_info = "Settlement"
                    if rep_entry.created_by:
                        source_info += f" by <@{rep_entry.created_by}>"
                elif rep_entry.source_type == 'admin':
                    source_info = "Admin"
                    if rep_entry.created_by:
                        source_info += f" by <@{rep_entry.created_by}>"
                else:
                    source_info = source_type

                history_lines.append(
                    f"**{date_str}** - {source_info}\n"
                    f"  {debtor_name} ↔ {creditor_name}: {amount:+d} tix\n"
                    f"  {rep_entry.notes or 'N/A'}"
                )

            # Split into multiple fields if needed (Discord limit)
            chunk_size = 5
            for i in range(0, len(history_lines), chunk_size):
                chunk = history_lines[i:i+chunk_size]
                field_name = "Recent History" if i == 0 else "More History"
                embed.add_field(
                    name=field_name,
                    value="\n\n".join(chunk),
                    inline=False
                )

            if len(entries) == limit:
                embed.set_footer(text=f"Showing {limit} most recent entries (there may be more)")

            logger.info(
                f"Admin {ctx.author.name} viewed debt history for guild {guild_id}"
                + (f" (filtered to player {player_id})" if player_id else "")
            )
            await ctx.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error fetching debt history: {e}")
            await ctx.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    def _format_balance(self, balance: int, player1: discord.User, player2: discord.User) -> str:
        """Format a balance description for display."""
        if balance == 0:
            return f"{player1.mention} and {player2.mention} were even"
        elif balance < 0:
            return f"{player1.mention} owed {player2.mention} **{abs(balance)} tix**"
        else:
            return f"{player2.mention} owed {player1.mention} **{abs(balance)} tix**"


def setup(bot):
    bot.add_cog(DebtAdminCommands(bot))
