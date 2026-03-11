"""
Admin debt management slash commands.

Commands:
- /debt-admin adjust - Manually adjust debt between two players (create, modify, forgive)
- /debt-admin stats - View comprehensive guild debt statistics
- /debt-admin history - View audit trail of admin debt modifications
- /debt-admin notify - DM players about their outstanding debts
"""
import asyncio
from datetime import datetime
import discord
from discord.ext import commands
from discord.commands import SlashCommandGroup, option
from loguru import logger

from services.debt_service import (
    adjust_debt,
    get_balance_with,
    get_guild_debt_stats,
    get_debt_history,
    get_all_balances_for,
    get_guild_debt_rows
)
from debt_views.helpers import get_member_name
from debt_views.settle_views import DMSettleDebtsView
from helpers.permissions import has_bot_manager_role
from utils import update_debt_summary_for_guild


class DebtNotifyConfirmView(discord.ui.View):
    """Confirmation view for sending debt notification DMs."""

    def __init__(self, notifications: list[tuple[str, str]], bot, guild_id: str):
        super().__init__(timeout=60)
        self.notifications = notifications  # [(player_id, message), ...]
        self.bot = bot
        self.guild_id = guild_id

    @discord.ui.button(label="Send Notifications", style=discord.ButtonStyle.green)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.disable_all_items()
        await interaction.response.edit_message(content="Sending notifications...", view=self)

        sent = 0
        failed = 0
        for player_id, msg in self.notifications:
            try:
                user = self.bot.get_user(int(player_id)) or await self.bot.fetch_user(int(player_id))
                view = DMSettleDebtsView(guild_id=self.guild_id, bot=self.bot)
                await user.send(msg, view=view)
                sent += 1
            except discord.Forbidden:
                failed += 1
            except discord.HTTPException as e:
                logger.warning(f"Failed to DM {player_id}: {e}")
                failed += 1
            await asyncio.sleep(0.5)

        result = f"Sent debt notifications to {sent} player{'s' if sent != 1 else ''}."
        if failed:
            result += f" {failed} failed (DMs disabled)."
        await interaction.edit_original_response(content=result, embed=None, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.disable_all_items()
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)

    async def on_timeout(self):
        self.disable_all_items()
        try:
            await self.message.edit(content="Timed out.", embed=None, view=None)
        except Exception:
            pass


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
    @option("older_than_days", int, description="Only show entries older than this many days (optional)", required=False, min_value=1)
    @has_bot_manager_role()
    async def debt_admin_history(
        self,
        ctx: discord.ApplicationContext,
        player: discord.User = None,
        limit: int = 25,
        older_than_days: int = None
    ):
        """View complete debt history (drafts, settlements, and admin modifications)."""
        await ctx.defer(ephemeral=True)

        try:
            guild_id = str(ctx.guild.id)
            player_id = str(player.id) if player else None

            entries = await get_debt_history(guild_id, player_id, limit, older_than_days)

            if not entries:
                filter_msg = f" for {player.display_name}" if player else ""
                if older_than_days:
                    filter_msg += f" older than {older_than_days} days"
                await ctx.followup.send(f"No debt history found{filter_msg}.", ephemeral=True)
                return

            # Build embed
            title = "Debt History"
            if player:
                title += f" - {player.display_name}"
            if older_than_days:
                title += f" (older than {older_than_days} days)"

            embed = discord.Embed(
                title=title,
                color=discord.Color.purple()
            )

            # Group entries by (source_id, debtor, creditor) to show each
            # debt pair separately — a single draft can produce multiple pairs.
            # We key on the sorted player pair so both sides collapse into one line.
            grouped_entries = {}
            for entry in entries:
                pair_key = (entry.source_id, *sorted([entry.player_id, entry.counterparty_id]))
                if pair_key not in grouped_entries:
                    grouped_entries[pair_key] = []
                grouped_entries[pair_key].append(entry)

            # Build history lines
            history_lines = []
            for pair_key, group in grouped_entries.items():
                # Take the first entry as representative (they share same timestamp, notes, etc.)
                rep_entry = group[0]
                if rep_entry.created_at:
                    date_str = rep_entry.created_at.strftime("%b %d, %Y %I:%M %p")
                    age_days = (datetime.utcnow() - rep_entry.created_at).days
                    date_str += f" ({age_days}d ago)"
                else:
                    date_str = "?"

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

    def _format_player_debt_message(self, guild: discord.Guild, player_id: str, balances: dict) -> str:
        """Format a DM message for a player about their debts."""
        you_owe = []
        owed_to_you = []

        for counterparty_id, balance in balances.items():
            name = get_member_name(guild, counterparty_id)
            if balance < 0:
                you_owe.append(f"**{abs(balance)} tix** to {name}")
            else:
                owed_to_you.append(f"**{balance} tix** from {name}")

        lines = [f"**Debt Reminder** from {guild.name}\n"]
        if you_owe:
            lines.append(f"You owe: {', '.join(you_owe)}")
        if owed_to_you:
            lines.append(f"Owed to you: {', '.join(owed_to_you)}")
        lines.append("\nUse /debts summary in the server to see details.")
        return "\n".join(lines)

    def _format_preview_lines(self, guild: discord.Guild, player_id: str, balances: dict) -> str:
        """Format preview lines for a single player's debts (for the admin embed)."""
        you_owe = []
        owed_to_you = []

        for counterparty_id, balance in balances.items():
            name = get_member_name(guild, counterparty_id)
            if balance < 0:
                you_owe.append(f"{abs(balance)} tix to {name}")
            else:
                owed_to_you.append(f"{balance} tix from {name}")

        lines = []
        if you_owe:
            lines.append(f"  You owe: {', '.join(you_owe)}")
        if owed_to_you:
            lines.append(f"  Owed to you: {', '.join(owed_to_you)}")
        return "\n".join(lines)

    @debt_admin.command(name="notify", description="[Admin] DM players about their outstanding debts")
    @option("player", discord.User, description="Notify a specific player (optional)", required=False)
    @has_bot_manager_role()
    async def debt_admin_notify(
        self,
        ctx: discord.ApplicationContext,
        player: discord.User = None
    ):
        """Send DM notifications to players about their outstanding debts."""
        await ctx.defer(ephemeral=True)

        try:
            guild_id = str(ctx.guild.id)

            if player:
                # Single player mode
                balances = await get_all_balances_for(guild_id, str(player.id))
                if not balances:
                    await ctx.followup.send(f"No outstanding debts found for {player.mention}.", ephemeral=True)
                    return
                player_balances = {str(player.id): balances}
            else:
                # All players mode - collect unique player IDs from debt rows
                debt_rows = await get_guild_debt_rows(guild_id)
                if not debt_rows:
                    await ctx.followup.send("No outstanding debts found in this server.", ephemeral=True)
                    return

                # Get unique player IDs involved in debts
                player_ids = set()
                for row in debt_rows:
                    player_ids.add(row.player_id)
                    player_ids.add(row.counterparty_id)

                # Fetch balances for each player
                player_balances = {}
                for pid in player_ids:
                    balances = await get_all_balances_for(guild_id, pid)
                    if balances:
                        player_balances[pid] = balances

            if not player_balances:
                await ctx.followup.send("No outstanding debts found.", ephemeral=True)
                return

            # Build notifications list and preview embed
            notifications = []
            preview_entries = []  # (name, preview_text)
            for pid, balances in player_balances.items():
                dm_msg = self._format_player_debt_message(ctx.guild, pid, balances)
                notifications.append((pid, dm_msg))
                name = get_member_name(ctx.guild, pid)
                preview_entries.append((name, self._format_preview_lines(ctx.guild, pid, balances)))

            count = len(notifications)
            embed = discord.Embed(
                title="Debt Notification Preview",
                description=f"{count} player{'s' if count != 1 else ''} will be notified",
                color=discord.Color.blue()
            )

            # Pack preview entries into embed fields, respecting the 1024 char field limit
            # and 25 field limit
            current_chunk = []
            current_len = 0
            field_count = 0
            for name, preview in preview_entries:
                entry = f"**{name}**\n{preview}"
                entry_len = len(entry) + 2  # +2 for "\n\n" separator
                if current_chunk and current_len + entry_len > 1024:
                    # Flush current chunk to a field
                    if field_count < 25:
                        embed.add_field(
                            name="Players" if field_count == 0 else "\u200b",
                            value="\n\n".join(current_chunk),
                            inline=False
                        )
                        field_count += 1
                    current_chunk = []
                    current_len = 0
                current_chunk.append(entry)
                current_len += entry_len

            if current_chunk and field_count < 25:
                embed.add_field(
                    name="Players" if field_count == 0 else "\u200b",
                    value="\n\n".join(current_chunk),
                    inline=False
                )
                field_count += 1

            if field_count >= 25:
                embed.set_footer(text="Preview truncated — all players will still be notified")

            view = DebtNotifyConfirmView(notifications, self.bot, str(ctx.guild.id))
            msg = await ctx.followup.send(embed=embed, view=view, ephemeral=True)
            view.message = msg

            logger.info(f"Admin {ctx.author.name} previewing debt notifications for {len(notifications)} players")

        except Exception as e:
            logger.error(f"Error in debt notify: {e}")
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
