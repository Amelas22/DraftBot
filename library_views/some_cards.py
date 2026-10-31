"""Borrowing only a few named cards from a drafted pool.

The panel's **Borrow only some cards** opens SomeCardsModal; what the drafter
typed is matched against their pool and read back to them with its hold
(describe_plan) before anything is reserved. Confirm then runs the borrow for
exactly the cards the read-back listed.
"""
from typing import Any, Optional

import discord
from loguru import logger
from typing_extensions import override

from helpers.money_gate import spawn_followup
from helpers.utils import ui_button
from library_views.callers import InteractionCaller
from services.card_lending_service import active_loan, card_count, library_behind
from services.library_loan_actions import (
    NOT_SET_UP, borrow_some, describe_plan, match_for_borrow)
from services.library_trust_service import hold_for

# The longest card name Magic prints is a double-faced name with its "//" and
# both halves; this leaves room for it.
_NAME_MAX = 141


class SomeCardsConfirmView(discord.ui.View):
    """Confirm or cancel the cards the read-back listed.

    The cards ride on the view as an argument rather than being re-read from
    anywhere, so what Confirm borrows is what the drafter was shown, and an
    abandoned view leaves nothing behind. A timeout does nothing.

    One press only. Both buttons check is_finished first: pycord drops a press
    on a stopped view, but two presses in the same tick are both dispatched
    before either callback has stopped it, and a second Confirm would borrow
    twice. Either press also takes the buttons off the message, so a stale
    Confirm is not left there to be pressed later.

    `origin` is the modal submission whose followup posted this message.
    """

    def __init__(self, cards: "list[dict[str, Any]]", session_id: str,
                 origin: "Optional[discord.Interaction]" = None) -> None:
        super().__init__(timeout=180)
        self.cards, self.session_id, self.origin = cards, session_id, origin

    @ui_button(label="Confirm", style=discord.ButtonStyle.primary)
    async def confirm(self, button: "discord.ui.Button[Any]",
                      interaction: discord.Interaction) -> None:
        if self.is_finished():
            return
        self.stop()
        spawn_followup("card-library borrow some", self._confirmed(interaction))

    async def _confirmed(self, interaction: discord.Interaction) -> None:
        # Deliberately not acknowledged here: borrow_some defers the
        # interaction itself, and a second acknowledgement would be refused.
        try:
            await borrow_some(InteractionCaller(interaction), self.session_id, self.cards)
        finally:
            await self._take_buttons_away(interaction)

    async def _take_buttons_away(self, interaction: discord.Interaction) -> None:
        """Clear the buttons through the webhook that posted the message.

        Not through this press: its one response is borrow_some's defer, and
        an ephemeral message cannot be edited through the channel. The modal
        submission's token outlives this view (15 minutes against 3), so it is
        still good. A miss only leaves a dead button behind -- pressing it
        again does nothing -- so it is logged, never raised over the borrow.
        """
        message = interaction.message
        if self.origin is None or message is None:
            return
        try:
            await self.origin.followup.edit_message(message.id, view=None)
        except discord.HTTPException:
            logger.warning("library: could not clear the borrow-some buttons on {}",
                           message.id)

    @ui_button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: "discord.ui.Button[Any]",
                     interaction: discord.Interaction) -> None:
        if self.is_finished():
            return
        self.stop()
        # Answered by editing the message the buttons sit on, which takes
        # them away in the same response.
        await interaction.response.edit_message(content="👌 Nothing borrowed.", view=None)


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
        view = SomeCardsConfirmView(plan.available, self.session_id, origin=interaction)
        hold = 0
        if plan.available:
            # Priced by the library the trade will charge through: a waiting
            # deck's own (library_behind prefers the one stamped on the loan),
            # else this server's, which is what library_behind gives for none.
            # match_for_borrow has already refused a deck from another draft.
            library = await library_behind(await active_loan(caller.user_id),
                                           caller.guild_id)
            held = await hold_for(library, caller.user_id,
                                  cards=card_count(plan.available))
            if held is None:
                await caller.send(NOT_SET_UP)
                return
            hold = held
        else:
            view.remove_item(view.confirm)
        await caller.send(describe_plan(plan, hold), view=view)
