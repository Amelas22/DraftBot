"""Borrowing only a few named cards from a drafted pool.

The panel's **Borrow only some cards** opens SomeCardsModal; what the drafter
typed is matched against their pool and read back to them with its hold
(describe_plan) before anything is reserved. Confirm then runs the borrow for
exactly the cards the read-back listed.
"""
from typing import Any

import discord
from typing_extensions import override

from helpers.money_gate import spawn_followup
from helpers.utils import ui_button
from library_views.callers import InteractionCaller
from services.library_loan_actions import borrow_some, describe_plan, match_for_borrow
from services.library_service import library_for
from services.library_trust_service import hold_for

_UNAVAILABLE = "📭 This server isn't set up to borrow from a card library."
# The longest card name Magic prints is a double-faced name with its "//" and
# both halves; this leaves room for it.
_NAME_MAX = 141


class SomeCardsConfirmView(discord.ui.View):
    """Confirm or cancel the cards the read-back listed.

    The cards ride on the view as an argument rather than being re-read from
    anywhere, so what Confirm borrows is what the drafter was shown, and an
    abandoned view leaves nothing behind. A timeout does nothing.
    """

    def __init__(self, cards: "list[dict[str, Any]]", session_id: str,
                 timeout: float = 180) -> None:
        super().__init__(timeout=timeout)
        self.cards, self.session_id = cards, session_id

    @ui_button(label="Confirm", style=discord.ButtonStyle.primary)
    async def confirm(self, button: "discord.ui.Button[Any]",
                      interaction: discord.Interaction) -> None:
        self.stop()
        # Deliberately not acknowledged here: borrow_some defers the
        # interaction itself, and a second acknowledgement would be refused.
        spawn_followup("card-library borrow some",
                       borrow_some(InteractionCaller(interaction),
                                   self.session_id, self.cards))

    @ui_button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: "discord.ui.Button[Any]",
                     interaction: discord.Interaction) -> None:
        await interaction.response.send_message("👌 Nothing borrowed.", ephemeral=True)
        self.stop()


class SomeCardsModal(discord.ui.Modal):
    """Up to three card names, the first required."""

    def __init__(self, session_id: str) -> None:
        super().__init__(
            discord.ui.InputText(label="Card 1", max_length=_NAME_MAX),
            discord.ui.InputText(label="Card 2", max_length=_NAME_MAX, required=False),
            discord.ui.InputText(label="Card 3", max_length=_NAME_MAX, required=False),
            title="Borrow only some cards")
        self.session_id = session_id

    @override
    async def callback(self, interaction: discord.Interaction) -> None:
        caller = InteractionCaller(interaction)
        await caller.defer()
        typed = [t for c in self.children
                 if (t := (getattr(c, "value", None) or "").strip())]
        plan = await match_for_borrow(caller.guild_id or 0, caller.user_id,
                                      self.session_id, typed)
        if isinstance(plan, str):
            await caller.send(plan)
            return
        copies = sum(int(c.get("qty") or 0) for c in plan.available)
        hold = 0
        if plan.available:
            held = await hold_for(await library_for(caller.guild_id),
                                  caller.user_id, cards=copies)
            if held is None:
                await caller.send(_UNAVAILABLE)
                return
            hold = held
        view = SomeCardsConfirmView(plan.available, self.session_id)
        if not plan.available:
            view.remove_item(view.confirm)
        await caller.send(describe_plan(plan, hold), view=view)
