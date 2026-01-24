"""
Settlement views for the debt tracking system.

Contains the [Settle Debts] button and related views for settling debts
from draft results.
"""
import asyncio
import traceback
import discord
from discord.ui import View, Button, Select, Modal, InputText
from loguru import logger

from services.debt_service import (
    get_all_balances_for,
    get_balance_with,
    get_entries_since_last_settlement,
    create_settlement
)
from .helpers import TRANSIENT_ERRORS, get_member_name, get_member_name_plain, format_entry_source, build_user_balance_embed


class SettleDebtsButton(Button):
    """Button that appears on victory messages for staked drafts."""

    def __init__(self, session_id: str, guild_id: str):
        super().__init__(
            label="Settle Debts",
            style=discord.ButtonStyle.success,
            custom_id=f"settle_debts:{session_id}",
            emoji="💰"
        )
        self.session_id = session_id
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        """Handle button click - show user's debts with participants."""
        user_id = str(interaction.user.id)
        logger.info(f"[SettleDebts] Button clicked by user {user_id} for session {self.session_id}")

        try:
            # Get all non-zero balances for this user
            logger.debug(f"[SettleDebts] Fetching balances for user {user_id} in guild {self.guild_id}")
            balances = await get_all_balances_for(
                guild_id=self.guild_id,
                player_id=user_id
            )
            logger.debug(f"[SettleDebts] Found balances: {balances}")

            if not balances:
                logger.info(f"[SettleDebts] No balances found for user {user_id}")
                await interaction.response.send_message(
                    "You have no outstanding debts with anyone.",
                    ephemeral=True
                )
                return

            embed = build_user_balance_embed(interaction.guild, balances)

            # Create dropdown to select counterparty
            view = CounterpartySelectView(
                user_id=user_id,
                guild_id=self.guild_id,
                balances=balances,
                guild=interaction.guild
            )

            logger.info(f"[SettleDebts] Sending balance embed with {len(balances)} counterparties")
            await interaction.response.send_message(
                embed=embed,
                view=view,
                ephemeral=True
            )
            logger.debug(f"[SettleDebts] Response sent successfully")

        except TRANSIENT_ERRORS as e:
            # Transient network errors - log and don't try to respond (will also fail)
            logger.warning(f"[SettleDebts] Transient network error (user can retry): {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"[SettleDebts] Error in button callback: {e}")
            logger.error(f"[SettleDebts] Traceback: {traceback.format_exc()}")
            try:
                await interaction.response.send_message(
                    "An error occurred. Please try again.",
                    ephemeral=True
                )
            except TRANSIENT_ERRORS:
                # If we can't even send the error message, just log it
                logger.warning(f"[SettleDebts] Could not send error message (interaction likely timed out)")
            except discord.errors.InteractionResponded:
                try:
                    await interaction.followup.send(
                        "An error occurred. Please try again.",
                        ephemeral=True
                    )
                except TRANSIENT_ERRORS:
                    logger.warning(f"[SettleDebts] Could not send followup error message")


class SettleDebtsView(View):
    """View containing the Settle Debts button for victory messages."""

    def __init__(self, session_id: str, guild_id: str, timeout: float = None):
        # No timeout for persistent views
        super().__init__(timeout=timeout)
        self.add_item(SettleDebtsButton(session_id=session_id, guild_id=guild_id))


class CounterpartySelectView(View):
    """View with dropdown to select which counterparty to settle with."""

    def __init__(self, user_id: str, guild_id: str, balances: dict, guild: discord.Guild):
        super().__init__(timeout=300)  # 5 minute timeout
        self.user_id = user_id
        self.guild_id = guild_id
        self.balances = balances
        self.guild = guild

        logger.debug(f"[CounterpartySelect] Creating view for user {user_id} with {len(balances)} counterparties")

        # Build select options
        options = []
        for counterparty_id, balance in balances.items():
            name = get_member_name_plain(guild, counterparty_id)  # Use plain name for dropdown

            # Determine direction
            if balance < 0:
                direction = f"You owe {abs(balance)} tix"
            else:
                direction = f"They owe you {balance} tix"

            options.append(discord.SelectOption(
                label=name[:100],  # Discord limit
                value=counterparty_id,
                description=direction[:100]
            ))

        # Limit to 25 options (Discord limit)
        options = options[:25]

        select = Select(
            placeholder="Select who to settle with...",
            options=options,
            custom_id="counterparty_select"
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        """Handle counterparty selection."""
        try:
            counterparty_id = interaction.data['values'][0]
            balance = self.balances.get(counterparty_id, 0)

            logger.info(f"[CounterpartySelect] User {self.user_id} selected counterparty {counterparty_id}, balance: {balance}")

            # Get breakdown of entries since last settlement
            logger.debug(f"[CounterpartySelect] Fetching entries since last settlement")
            entries = await get_entries_since_last_settlement(
                guild_id=self.guild_id,
                player_id=self.user_id,
                counterparty_id=counterparty_id
            )
            logger.debug(f"[CounterpartySelect] Found {len(entries)} entries")

            name_decorated = get_member_name(self.guild, counterparty_id)
            name_plain = get_member_name_plain(self.guild, counterparty_id)

            # Build breakdown embed
            embed = discord.Embed(
                title=f"Settle with {name_plain}",
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
                for entry in entries[-10:]:  # Last 10 entries
                    source = format_entry_source(entry)

                    if entry.amount < 0:
                        breakdown_lines.append(f"{source}: You owe {abs(entry.amount)} tix")
                    else:
                        breakdown_lines.append(f"{source}: They owe you {entry.amount} tix")

                if len(entries) > 10:
                    breakdown_lines.append(f"... and {len(entries) - 10} more")

                embed.add_field(
                    name="Breakdown (since last settlement)",
                    value="\n".join(breakdown_lines) or "No entries",
                    inline=False
                )

            embed.set_footer(text="Click 'Enter Amount' to confirm the payment amount")

            # Create view with amount input button
            view = AmountInputView(
                user_id=self.user_id,
                guild_id=self.guild_id,
                counterparty_id=counterparty_id,
                net_balance=balance,
                counterparty_name_plain=name_plain,
                counterparty_name_decorated=name_decorated
            )

            logger.debug(f"[CounterpartySelect] Editing message with breakdown embed")
            await interaction.response.edit_message(embed=embed, view=view)
            logger.debug(f"[CounterpartySelect] Message edited successfully")

        except Exception as e:
            logger.error(f"[CounterpartySelect] Error in select callback: {e}")
            logger.error(f"[CounterpartySelect] Traceback: {traceback.format_exc()}")
            try:
                await interaction.response.send_message(
                    f"An error occurred: {str(e)}",
                    ephemeral=True
                )
            except discord.errors.InteractionResponded:
                await interaction.followup.send(
                    f"An error occurred: {str(e)}",
                    ephemeral=True
                )


class AmountInputView(View):
    """View with button to enter settlement amount."""

    def __init__(self, user_id: str, guild_id: str, counterparty_id: str,
                 net_balance: int, counterparty_name_plain: str,
                 counterparty_name_decorated: str):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.guild_id = guild_id
        self.counterparty_id = counterparty_id
        self.net_balance = net_balance
        self.counterparty_name_plain = counterparty_name_plain
        self.counterparty_name_decorated = counterparty_name_decorated
        logger.debug(f"[AmountInputView] Created for user {user_id}, counterparty {counterparty_id}, balance {net_balance}")

    @discord.ui.button(label="Enter Amount", style=discord.ButtonStyle.primary)
    async def enter_amount(self, button: Button, interaction: discord.Interaction):
        """Open modal for amount input."""
        try:
            logger.info(f"[AmountInputView] Enter Amount clicked by user {interaction.user.id}")
            modal = AmountConfirmationModal(
                user_id=self.user_id,
                guild_id=self.guild_id,
                counterparty_id=self.counterparty_id,
                net_balance=self.net_balance,
                counterparty_name_plain=self.counterparty_name_plain,
                counterparty_name_decorated=self.counterparty_name_decorated
            )
            logger.debug(f"[AmountInputView] Sending modal")
            await interaction.response.send_modal(modal)
            logger.debug(f"[AmountInputView] Modal sent successfully")
        except Exception as e:
            logger.error(f"[AmountInputView] Error opening modal: {e}")
            logger.error(f"[AmountInputView] Traceback: {traceback.format_exc()}")
            try:
                await interaction.response.send_message(
                    f"An error occurred: {str(e)}",
                    ephemeral=True
                )
            except discord.errors.InteractionResponded:
                await interaction.followup.send(
                    f"An error occurred: {str(e)}",
                    ephemeral=True
                )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: Button, interaction: discord.Interaction):
        """Cancel settlement."""
        logger.info(f"[AmountInputView] Cancel clicked by user {interaction.user.id}")
        await interaction.response.edit_message(
            content="Settlement cancelled.",
            embed=None,
            view=None
        )


class AmountConfirmationModal(Modal):
    """Modal for entering the settlement amount."""

    def __init__(self, user_id: str, guild_id: str, counterparty_id: str,
                 net_balance: int, counterparty_name_plain: str,
                 counterparty_name_decorated: str):
        # Use plain name (no icons) for modal title with safe truncation
        # Discord modal title limit: 45 chars, "Settle with " prefix: 12 chars
        # Available for name: 33 chars, reserve 3 for ellipsis if needed
        if len(counterparty_name_plain) <= 33:
            safe_name = counterparty_name_plain
        else:
            safe_name = counterparty_name_plain[:30] + "..."

        super().__init__(title=f"Settle with {safe_name}")
        self.user_id = user_id
        self.guild_id = guild_id
        self.counterparty_id = counterparty_id
        self.net_balance = net_balance
        self.counterparty_name_plain = counterparty_name_plain
        self.counterparty_name_decorated = counterparty_name_decorated

        logger.debug(f"[AmountModal] Created for user {user_id}, counterparty {counterparty_id}, balance {net_balance}")

        # Pre-fill with absolute net balance
        default_amount = str(abs(net_balance))

        self.amount_input = InputText(
            label="Amount paid/received (tix)",
            placeholder=f"Enter amount (net balance: {abs(net_balance)} tix)",
            value=default_amount,
            required=True,
            max_length=10
        )
        self.add_item(self.amount_input)

    async def callback(self, interaction: discord.Interaction):
        """Handle modal submission (py-cord uses 'callback' not 'on_submit')."""
        logger.info(f"[AmountModal] Modal submitted by user {interaction.user.id}")
        logger.debug(f"[AmountModal] Amount input value: '{self.amount_input.value}'")

        try:
            amount = int(self.amount_input.value)
            logger.debug(f"[AmountModal] Parsed amount: {amount}")
            if amount <= 0:
                logger.warning(f"[AmountModal] Invalid amount (non-positive): {amount}")
                await interaction.response.send_message(
                    "Amount must be positive.",
                    ephemeral=True
                )
                return
        except ValueError as e:
            logger.warning(f"[AmountModal] Invalid amount (not a number): '{self.amount_input.value}' - {e}")
            await interaction.response.send_message(
                "Please enter a valid number.",
                ephemeral=True
            )
            return

        # Determine who paid whom based on the original net balance
        # If net_balance < 0, user owes counterparty (user is payer)
        # If net_balance > 0, counterparty owes user (counterparty is payer)
        if self.net_balance < 0:
            payer_id = self.user_id
            payee_id = self.counterparty_id
            direction = "you paid"
        else:
            payer_id = self.counterparty_id
            payee_id = self.user_id
            direction = "you received"

        logger.debug(f"[AmountModal] Settlement direction: payer={payer_id}, payee={payee_id}, direction='{direction}'")

        # Check if amount matches
        abs_balance = abs(self.net_balance)
        if amount == abs_balance:
            match_text = "Amount matches net balance."
            color = discord.Color.green()
        else:
            diff = abs_balance - amount
            if diff > 0:
                match_text = f"This is a partial payment. {diff} tix will remain."
            else:
                match_text = f"Amount exceeds net balance by {abs(diff)} tix."
            color = discord.Color.orange()

        # Create confirmation embed
        embed = discord.Embed(
            title="Confirm Settlement",
            color=color
        )
        embed.add_field(
            name="Settlement Details",
            value=(
                f"**With:** {self.counterparty_name_decorated}\n"
                f"**Amount:** {amount} tix ({direction})\n"
                f"**Net balance was:** {abs_balance} tix"
            ),
            inline=False
        )
        embed.add_field(
            name="Status",
            value=match_text,
            inline=False
        )

        # Calculate new balance after settlement
        if self.net_balance < 0:
            # User owes, payment reduces their debt
            new_balance = self.net_balance + amount
        else:
            # User is owed, payment reduces their credit
            new_balance = self.net_balance - amount

        if new_balance == 0:
            new_balance_text = "0 tix (settled)"
        elif new_balance < 0:
            new_balance_text = f"You will owe {abs(new_balance)} tix"
        else:
            new_balance_text = f"They will owe you {new_balance} tix"

        embed.add_field(
            name="New Balance",
            value=new_balance_text,
            inline=False
        )

        view = SettlementConfirmView(
            user_id=self.user_id,
            guild_id=self.guild_id,
            payer_id=payer_id,
            payee_id=payee_id,
            amount=amount
        )

        logger.debug(f"[AmountModal] Sending confirmation embed")
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        logger.debug(f"[AmountModal] Confirmation sent successfully")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        """Handle errors in modal submission."""
        logger.error(f"[AmountModal] Error in on_submit: {error}")
        logger.error(f"[AmountModal] Traceback: {traceback.format_exc()}")
        try:
            await interaction.response.send_message(
                f"An error occurred: {str(error)}",
                ephemeral=True
            )
        except discord.errors.InteractionResponded:
            await interaction.followup.send(
                f"An error occurred: {str(error)}",
                ephemeral=True
            )


class SettlementConfirmView(View):
    """Final confirmation view before creating settlement."""

    def __init__(self, user_id: str, guild_id: str, payer_id: str, payee_id: str, amount: int):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.guild_id = guild_id
        self.payer_id = payer_id
        self.payee_id = payee_id
        self.amount = amount
        self._processing = False  # Guard against double-clicks

        # Generate unique settlement_id for this settlement flow
        # Double-clicks within the same flow use the same ID (idempotent)
        import uuid
        self.settlement_id = str(uuid.uuid4())

        logger.debug(f"[SettlementConfirm] Created: user={user_id}, payer={payer_id}, payee={payee_id}, amount={amount}, settlement_id={self.settlement_id}")

    @discord.ui.button(label="Confirm Settlement", style=discord.ButtonStyle.success)
    async def confirm(self, button: Button, interaction: discord.Interaction):
        """Create the settlement."""
        # Guard against double-clicks
        if self._processing:
            logger.warning(f"[SettlementConfirm] Ignoring duplicate click from user {interaction.user.id}")
            await interaction.response.send_message(
                "Settlement is already being processed...",
                ephemeral=True
            )
            return

        self._processing = True
        logger.info(f"[SettlementConfirm] Confirm clicked by user {interaction.user.id}")

        # Defer immediately to avoid Discord's 3-second timeout
        # Other async operations (like leaderboard updates) can delay us
        await interaction.response.defer()

        # Disable buttons immediately to prevent double-clicks
        for item in self.children:
            item.disabled = True

        try:
            # Update the message to show processing state
            await interaction.edit_original_response(
                content="Processing settlement...",
                view=self
            )
        except Exception as e:
            logger.warning(f"[SettlementConfirm] Could not update to processing state: {e}")

        logger.info(f"[SettlementConfirm] Creating settlement: guild={self.guild_id}, payer={self.payer_id}, payee={self.payee_id}, amount={self.amount}")

        try:
            result = await create_settlement(
                guild_id=self.guild_id,
                payer_id=self.payer_id,
                payee_id=self.payee_id,
                amount=self.amount,
                settled_by=self.user_id,
                settlement_id=self.settlement_id
            )
            logger.info(f"[SettlementConfirm] Settlement created successfully: {result}")

            await interaction.edit_original_response(
                content=f"Settlement of {self.amount} tix recorded successfully!",
                embed=None,
                view=None
            )
            logger.info(f"[SettlementConfirm] Success message sent")

            # Update debt summary in background (lazy import to avoid circular import)
            try:
                from utils import update_debt_summary_for_guild
                asyncio.create_task(update_debt_summary_for_guild(interaction.client, self.guild_id))
            except Exception as e:
                logger.warning(f"[SettlementConfirm] Failed to trigger debt summary update: {e}")

        except Exception as e:
            logger.error(f"[SettlementConfirm] Failed to create settlement: {e}")
            logger.error(f"[SettlementConfirm] Traceback: {traceback.format_exc()}")
            self._processing = False  # Allow retry on error
            # Re-enable buttons on error
            for item in self.children:
                item.disabled = False
            try:
                await interaction.edit_original_response(
                    content=f"Failed to record settlement: {str(e)}",
                    view=self
                )
            except Exception:
                pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: Button, interaction: discord.Interaction):
        """Cancel settlement."""
        if self._processing:
            await interaction.response.send_message(
                "Settlement is already being processed, cannot cancel.",
                ephemeral=True
            )
            return

        logger.info(f"[SettlementConfirm] Cancel clicked by user {interaction.user.id}")
        await interaction.response.edit_message(
            content="Settlement cancelled.",
            embed=None,
            view=None
        )


class PublicSettleDebtsView(View):
    """Persistent view for public debt summary messages with settle button."""

    def __init__(self):
        super().__init__(timeout=None)  # Persistent view

    @discord.ui.button(
        label="Settle My Debts",
        style=discord.ButtonStyle.primary,
        custom_id="public_settle_debts_button",
        emoji="\U0001f4b0"
    )
    async def settle_button(self, button: Button, interaction: discord.Interaction):
        """Handle button click - show user's debts and launch settlement flow."""
        user_id = str(interaction.user.id)
        guild_id = str(interaction.guild.id)
        logger.info(f"[PublicSettle] Button clicked by user {user_id} in guild {guild_id}")

        try:
            # Get all non-zero balances for this user
            balances = await get_all_balances_for(
                guild_id=guild_id,
                player_id=user_id
            )

            if not balances:
                await interaction.response.send_message(
                    "You have no outstanding debts with anyone.",
                    ephemeral=True
                )
                return

            embed = build_user_balance_embed(interaction.guild, balances)

            # Create dropdown to select counterparty
            view = CounterpartySelectView(
                user_id=user_id,
                guild_id=guild_id,
                balances=balances,
                guild=interaction.guild
            )

            await interaction.response.send_message(
                embed=embed,
                view=view,
                ephemeral=True
            )

        except TRANSIENT_ERRORS as e:
            logger.warning(f"[PublicSettle] Transient network error: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"[PublicSettle] Error in button callback: {e}")
            logger.error(f"[PublicSettle] Traceback: {traceback.format_exc()}")
            try:
                await interaction.response.send_message(
                    "An error occurred. Please try again.",
                    ephemeral=True
                )
            except discord.errors.InteractionResponded:
                await interaction.followup.send(
                    "An error occurred. Please try again.",
                    ephemeral=True
                )

    def to_metadata(self):
        """Return metadata for persistent storage."""
        return {"view_type": "debt_summary"}
