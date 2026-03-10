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
    create_settlement,
    get_transferable_debtors,
    create_debt_transfer
)
from .helpers import TRANSIENT_ERRORS, get_member_name, get_member_name_plain, format_entry_source, build_user_balance_embed


async def _build_settle_entry_view(
    user_id: str, guild_id: str, balances: dict, guild: discord.Guild
) -> View:
    """Build the appropriate entry view: SettleOrTransferView if transfers are available, else CounterpartySelectView."""
    for cid, bal in balances.items():
        if bal < 0:
            transferable = await get_transferable_debtors(
                guild_id=guild_id, transferrer_id=user_id, creditor_id=cid
            )
            if transferable:
                return SettleOrTransferView(
                    user_id=user_id, guild_id=guild_id, balances=balances, guild=guild
                )
    return CounterpartySelectView(
        user_id=user_id, guild_id=guild_id, balances=balances, guild=guild
    )


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
            view = await _build_settle_entry_view(
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

class SettleOrTransferView(View):
    """View that asks the user whether they want to settle or transfer debt."""

    def __init__(self, user_id: str, guild_id: str, balances: dict, guild: discord.Guild):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.guild_id = guild_id
        self.balances = balances
        self.guild = guild

    @discord.ui.button(label="Settle a Debt", style=discord.ButtonStyle.primary, emoji="\U0001f4b0")
    async def settle_button(self, button: Button, interaction: discord.Interaction):
        """Go to the normal settle flow."""
        try:
            embed = build_user_balance_embed(self.guild, self.balances)
            view = CounterpartySelectView(
                user_id=self.user_id,
                guild_id=self.guild_id,
                balances=self.balances,
                guild=self.guild
            )
            await interaction.response.edit_message(embed=embed, view=view)
        except TRANSIENT_ERRORS as e:
            logger.warning(f"[SettleOrTransfer] Transient error in settle button: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"[SettleOrTransfer] Error in settle button: {e}")
            logger.error(f"[SettleOrTransfer] Traceback: {traceback.format_exc()}")
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

    @discord.ui.button(label="Transfer Debt", style=discord.ButtonStyle.secondary, emoji="\U0001f504")
    async def transfer_button(self, button: Button, interaction: discord.Interaction):
        """Go to the transfer flow - pick who you owe first."""
        try:
            # Re-fetch live balances (may have changed since view was created)
            balances = await get_all_balances_for(
                guild_id=self.guild_id,
                player_id=self.user_id
            )

            # Find all creditors (people the user owes) that have transferable debtors
            creditors_with_transfers = []
            for counterparty_id, balance in balances.items():
                if balance >= 0:
                    continue  # User doesn't owe this person
                transferable = await get_transferable_debtors(
                    guild_id=self.guild_id,
                    transferrer_id=self.user_id,
                    creditor_id=counterparty_id
                )
                if transferable:
                    creditors_with_transfers.append(
                        (counterparty_id, abs(balance), transferable)
                    )

            if not creditors_with_transfers:
                await interaction.response.edit_message(
                    content="No transferable debts found. No one owes you money that could be redirected.",
                    embed=None,
                    view=None
                )
                return

            view = TransferCreditorSelectView(
                user_id=self.user_id,
                guild_id=self.guild_id,
                creditors_with_transfers=creditors_with_transfers,
                guild=self.guild
            )

            embed = discord.Embed(
                title="Transfer Debt",
                description=(
                    "Select which debt you want to transfer.\n\n"
                    "This lets you redirect someone else's debt to you "
                    "toward someone you owe."
                ),
                color=discord.Color.blurple()
            )

            await interaction.response.edit_message(embed=embed, view=view)

        except TRANSIENT_ERRORS as e:
            logger.warning(f"[SettleOrTransfer] Transient error in transfer button: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"[SettleOrTransfer] Error in transfer button: {e}")
            logger.error(f"[SettleOrTransfer] Traceback: {traceback.format_exc()}")
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


class TransferCreditorSelectView(View):
    """View with dropdown to select which creditor (person you owe) to transfer debt toward."""

    def __init__(self, user_id: str, guild_id: str,
                 creditors_with_transfers: list, guild: discord.Guild):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.guild_id = guild_id
        self.guild = guild

        # Store transferable debtors per creditor for lookup after selection
        self._creditor_lookup = {}

        options = []
        for creditor_id, amount_owed, transferable_debtors in creditors_with_transfers:
            name = get_member_name_plain(guild, creditor_id)
            self._creditor_lookup[creditor_id] = transferable_debtors

            options.append(discord.SelectOption(
                label=name[:100],
                value=creditor_id,
                description=f"You owe them {amount_owed} tix"[:100]
            ))

        options = options[:25]

        select = Select(
            placeholder="Select whose debt to reduce...",
            options=options,
            custom_id="transfer_creditor_select"
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        """Handle creditor selection - show debtor dropdown."""
        try:
            creditor_id = interaction.data['values'][0]
            transferable_debtors = self._creditor_lookup[creditor_id]
            creditor_name_plain = get_member_name_plain(self.guild, creditor_id)
            creditor_name_decorated = get_member_name(self.guild, creditor_id)

            logger.info(
                f"[TransferCreditorSelect] User {self.user_id} selected creditor {creditor_id}, "
                f"{len(transferable_debtors)} transferable debtors"
            )

            view = DebtorSelectView(
                user_id=self.user_id,
                guild_id=self.guild_id,
                creditor_id=creditor_id,
                creditor_name_plain=creditor_name_plain,
                creditor_name_decorated=creditor_name_decorated,
                transferable_debtors=transferable_debtors,
                guild=self.guild
            )

            embed = discord.Embed(
                title="Transfer Debt",
                description=(
                    f"Select someone who owes you to transfer their debt to "
                    f"{creditor_name_decorated}."
                ),
                color=discord.Color.blurple()
            )

            await interaction.response.edit_message(embed=embed, view=view)

        except TRANSIENT_ERRORS as e:
            logger.warning(f"[TransferCreditorSelect] Transient error: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"[TransferCreditorSelect] Error: {e}")
            logger.error(f"[TransferCreditorSelect] Traceback: {traceback.format_exc()}")
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


class DebtorSelectView(View):
    """View with dropdown to select which debtor to transfer debt from."""

    def __init__(self, user_id: str, guild_id: str, creditor_id: str,
                 creditor_name_plain: str, creditor_name_decorated: str,
                 transferable_debtors: list, guild: discord.Guild):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.guild_id = guild_id
        self.creditor_id = creditor_id
        self.creditor_name_plain = creditor_name_plain
        self.creditor_name_decorated = creditor_name_decorated
        self.transferable_debtors = transferable_debtors
        self.guild = guild

        # Build a lookup for easy access after selection
        self._debtor_lookup = {}

        options = []
        for debtor_id, amount_owed, max_transfer in transferable_debtors:
            name = get_member_name_plain(guild, debtor_id)
            self._debtor_lookup[debtor_id] = (amount_owed, max_transfer)

            options.append(discord.SelectOption(
                label=name[:100],
                value=debtor_id,
                description=f"Owes you {amount_owed} tix (max transfer: {max_transfer} tix)"[:100]
            ))

        options = options[:25]

        select = Select(
            placeholder="Select who to transfer debt from...",
            options=options,
            custom_id="debtor_select"
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        """Handle debtor selection - open transfer amount modal."""
        try:
            debtor_id = interaction.data['values'][0]
            amount_owed, max_transfer = self._debtor_lookup[debtor_id]
            debtor_name_plain = get_member_name_plain(self.guild, debtor_id)
            debtor_name_decorated = get_member_name(self.guild, debtor_id)

            logger.info(
                f"[DebtorSelect] User {self.user_id} selected debtor {debtor_id}, "
                f"owes {amount_owed}, max transfer {max_transfer}"
            )

            modal = TransferAmountModal(
                user_id=self.user_id,
                guild_id=self.guild_id,
                debtor_id=debtor_id,
                creditor_id=self.creditor_id,
                debtor_name_plain=debtor_name_plain,
                debtor_name_decorated=debtor_name_decorated,
                creditor_name_plain=self.creditor_name_plain,
                creditor_name_decorated=self.creditor_name_decorated,
                max_amount=max_transfer,
                guild=self.guild
            )

            await interaction.response.send_modal(modal)

        except TRANSIENT_ERRORS as e:
            logger.warning(f"[DebtorSelect] Transient error: {type(e).__name__}: {e}")
        except Exception as e:
            logger.error(f"[DebtorSelect] Error: {e}")
            logger.error(f"[DebtorSelect] Traceback: {traceback.format_exc()}")
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


class TransferAmountModal(Modal):
    """Modal for entering the transfer amount."""

    def __init__(self, user_id: str, guild_id: str, debtor_id: str,
                 creditor_id: str, debtor_name_plain: str,
                 debtor_name_decorated: str, creditor_name_plain: str,
                 creditor_name_decorated: str, max_amount: int,
                 guild: discord.Guild):
        # Safe title truncation
        title_text = f"Transfer: {debtor_name_plain} → {creditor_name_plain}"
        if len(title_text) > 45:
            title_text = title_text[:42] + "..."

        super().__init__(title=title_text)
        self.user_id = user_id
        self.guild_id = guild_id
        self.debtor_id = debtor_id
        self.creditor_id = creditor_id
        self.debtor_name_plain = debtor_name_plain
        self.debtor_name_decorated = debtor_name_decorated
        self.creditor_name_plain = creditor_name_plain
        self.creditor_name_decorated = creditor_name_decorated
        self.max_amount = max_amount
        self.guild = guild

        self.amount_input = InputText(
            label=f"Amount to transfer (max: {max_amount} tix)",
            placeholder=f"Enter amount (max: {max_amount} tix)",
            value=str(max_amount),
            required=True,
            max_length=10
        )
        self.add_item(self.amount_input)

    async def callback(self, interaction: discord.Interaction):
        """Handle modal submission."""
        logger.info(f"[TransferModal] Submitted by user {interaction.user.id}")

        try:
            amount = int(self.amount_input.value)
            if amount <= 0:
                await interaction.response.send_message(
                    "Amount must be positive.",
                    ephemeral=True
                )
                return
            if amount > self.max_amount:
                await interaction.response.send_message(
                    f"Amount cannot exceed {self.max_amount} tix.",
                    ephemeral=True
                )
                return
        except ValueError:
            await interaction.response.send_message(
                "Please enter a valid number.",
                ephemeral=True
            )
            return

        # Get current balances for the confirmation display
        debtor_owes_transferrer = abs(await get_balance_with(
            self.guild_id, self.debtor_id, self.user_id
        ))
        transferrer_owes_creditor = abs(await get_balance_with(
            self.guild_id, self.user_id, self.creditor_id
        ))

        # Guard against stale data — balances may have changed since the modal opened
        if amount > debtor_owes_transferrer or amount > transferrer_owes_creditor:
            await interaction.response.send_message(
                "The debt balances have changed since you started this flow. Please try again.",
                ephemeral=True
            )
            return

        # Build confirmation embed
        embed = discord.Embed(
            title="Confirm Debt Transfer",
            description="This will rearrange the following debts:",
            color=discord.Color.blurple()
        )

        embed.add_field(
            name="What will happen",
            value=(
                f"• {self.debtor_name_decorated} owes you **{debtor_owes_transferrer}** tix → "
                f"reduced by **{amount}** to **{debtor_owes_transferrer - amount}** tix\n"
                f"• You owe {self.creditor_name_decorated} **{transferrer_owes_creditor}** tix → "
                f"reduced by **{amount}** to **{transferrer_owes_creditor - amount}** tix\n"
                f"• {self.debtor_name_decorated} will owe {self.creditor_name_decorated} **{amount}** tix"
            ),
            inline=False
        )

        embed.set_footer(text="Net debts in the system remain the same — only who owes whom changes.")

        view = TransferConfirmView(
            user_id=self.user_id,
            guild_id=self.guild_id,
            debtor_id=self.debtor_id,
            creditor_id=self.creditor_id,
            amount=amount,
            guild=self.guild
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"[TransferModal] Error: {error}")
        logger.error(f"[TransferModal] Traceback: {traceback.format_exc()}")
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


class TransferConfirmView(View):
    """Final confirmation view before executing debt transfer."""

    def __init__(self, user_id: str, guild_id: str, debtor_id: str,
                 creditor_id: str, amount: int, guild: discord.Guild):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.guild_id = guild_id
        self.debtor_id = debtor_id
        self.creditor_id = creditor_id
        self.amount = amount
        self.guild = guild
        self._processing = False

        import uuid
        self.transfer_id = str(uuid.uuid4())

        logger.debug(
            f"[TransferConfirm] Created: transferrer={user_id}, debtor={debtor_id}, "
            f"creditor={creditor_id}, amount={amount}, transfer_id={self.transfer_id}"
        )

    @discord.ui.button(label="Confirm Transfer", style=discord.ButtonStyle.success)
    async def confirm(self, button: Button, interaction: discord.Interaction):
        """Execute the debt transfer."""
        if self._processing:
            logger.warning(f"[TransferConfirm] Ignoring duplicate click from user {interaction.user.id}")
            await interaction.response.send_message(
                "Transfer is already being processed...",
                ephemeral=True
            )
            return

        self._processing = True
        logger.info(f"[TransferConfirm] Confirm clicked by user {interaction.user.id}")

        await interaction.response.defer()

        for item in self.children:
            item.disabled = True

        try:
            await interaction.edit_original_response(
                content="Processing transfer...",
                view=self
            )
        except Exception as e:
            logger.warning(f"[TransferConfirm] Could not update to processing state: {e}")

        try:
            await create_debt_transfer(
                guild_id=self.guild_id,
                transferrer_id=self.user_id,
                debtor_id=self.debtor_id,
                creditor_id=self.creditor_id,
                amount=self.amount,
                transfer_id=self.transfer_id
            )

            debtor_name = get_member_name(self.guild, self.debtor_id)
            creditor_name = get_member_name(self.guild, self.creditor_id)

            await interaction.edit_original_response(
                content=(
                    f"Debt transfer of {self.amount} tix recorded successfully!\n"
                    f"{debtor_name} now owes {creditor_name} instead of you."
                ),
                embed=None,
                view=None
            )
            logger.info(f"[TransferConfirm] Transfer completed successfully")

            # Update debt summary in background
            try:
                from utils import update_debt_summary_for_guild
                asyncio.create_task(update_debt_summary_for_guild(interaction.client, self.guild_id))
            except Exception as e:
                logger.warning(f"[TransferConfirm] Failed to trigger debt summary update: {e}")

        except Exception as e:
            logger.error(f"[TransferConfirm] Failed to create transfer: {e}")
            logger.error(f"[TransferConfirm] Traceback: {traceback.format_exc()}")
            self._processing = False
            for item in self.children:
                item.disabled = False
            try:
                await interaction.edit_original_response(
                    content=f"Failed to record transfer: {str(e)}",
                    view=self
                )
            except Exception:
                pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: Button, interaction: discord.Interaction):
        """Cancel transfer."""
        if self._processing:
            await interaction.response.send_message(
                "Transfer is already being processed, cannot cancel.",
                ephemeral=True
            )
            return

        logger.info(f"[TransferConfirm] Cancel clicked by user {interaction.user.id}")
        await interaction.response.edit_message(
            content="Transfer cancelled.",
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
    """Persistent view for public debt summary messages with settle button and pagination."""

    def __init__(self, pages=None):
        super().__init__(timeout=None)  # Persistent view
        # Pages are only used for the initial send; after that, buttons are stateless
        self._initial_pages = pages or []
        self._update_pagination_buttons_for_pages(self._initial_pages, 0)

    def _update_pagination_buttons_for_pages(self, pages, current_page):
        """Enable/disable pagination buttons based on page state."""
        has_pages = len(pages) > 1
        self.prev_button.disabled = not has_pages or current_page <= 0
        self.next_button.disabled = not has_pages or current_page >= len(pages) - 1

    @staticmethod
    def _get_current_page_from_embed(interaction: discord.Interaction):
        """Parse current page number from the embed footer (0-indexed)."""
        message = interaction.message
        if message and message.embeds:
            footer = message.embeds[0].footer
            if footer and footer.text and "Page " in footer.text:
                try:
                    # Footer format: "Page X of Y (Z total debts)"
                    page_str = footer.text.split("Page ")[1].split(" of ")[0]
                    return int(page_str) - 1  # Convert to 0-indexed
                except (IndexError, ValueError):
                    pass
        return 0

    async def _get_pages_and_navigate(self, interaction: discord.Interaction, direction: int):
        """Fetch fresh pages and navigate. direction: -1 for prev, +1 for next."""
        guild = interaction.guild
        if not guild:
            await interaction.response.defer()
            return

        from services.debt_service import get_guild_debt_rows
        from debt_views.helpers import build_guild_debt_embed_pages

        rows = await get_guild_debt_rows(str(guild.id))
        pages = build_guild_debt_embed_pages(guild, rows)

        current_page = self._get_current_page_from_embed(interaction)
        new_page = current_page + direction
        new_page = max(0, min(new_page, len(pages) - 1))

        # Build a fresh view with correct button states for this page
        view = PublicSettleDebtsView(pages=pages)
        view._update_pagination_buttons_for_pages(pages, new_page)

        await interaction.response.edit_message(embed=pages[new_page], view=view)

    @discord.ui.button(
        label="\u25c0\ufe0f",
        style=discord.ButtonStyle.blurple,
        custom_id="debt_summary_prev_page",
        row=0
    )
    async def prev_button(self, button: Button, interaction: discord.Interaction):
        """Go to previous page."""
        await self._get_pages_and_navigate(interaction, -1)

    @discord.ui.button(
        label="\u25b6\ufe0f",
        style=discord.ButtonStyle.blurple,
        custom_id="debt_summary_next_page",
        row=0
    )
    async def next_button(self, button: Button, interaction: discord.Interaction):
        """Go to next page."""
        await self._get_pages_and_navigate(interaction, +1)

    @discord.ui.button(
        label="Settle My Debts",
        style=discord.ButtonStyle.primary,
        custom_id="public_settle_debts_button",
        emoji="\U0001f4b0",
        row=1
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
            view = await _build_settle_entry_view(
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
