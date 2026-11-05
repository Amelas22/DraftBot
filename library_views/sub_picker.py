"""The picker behind the library panel's **Sub for a player**.

Who can't play is chosen from the presser's own team, the presser included
(a bot manager outside the draft sees every seated player, and the team
follows from the pick); who is subbing is any member of the server. Discord
modals cannot hold a user select, so this is an ephemeral message with
components, and Confirm does the work.

Confirm runs the checks, the offer and then, for a sub from outside the
draft, the chat access under the draft's panel lock. Once the lock is
released it syncs the panel and DMs the sub that the deck is waiting
(services.library_sub's lock contract). The grant comes
after the offer because its announcement says the deck has been offered: a
refused offer grants nothing and announces nothing.
"""
from typing import Any, Optional

import discord
from loguru import logger
from sqlalchemy import select

from database.db_session import AsyncSessionLocal
from helpers.display_names import get_display_name
from helpers.substitutes import GrantDecision, grant_sub_access, resolve_sub_grant
from helpers.utils import PrefilledSelect, not_none, ui_button
from models.draft_session import DraftSession
from services.draft_deck_assignment import _say_it_is_ready
from services.library_panel import _lock_for, nudge
from services.library_sub import (
    chat_line, check_sub, confirm_reply, offer_to_sub, sub_context)

NO_GRANT = " ⚠️ Could not grant chat access — add them with /add_sub."
WENT_WRONG = "📭 Something went wrong — try again."


def _ids(team: Any) -> "list[str]":
    return [str(p) for p in (team or [])]


async def _acknowledge(interaction: discord.Interaction) -> None:
    """A pick is kept on its select for Confirm; it needs no message."""
    await interaction.response.defer()


class SubPickerView(discord.ui.View):
    """Who can't play, who is subbing, Confirm and Cancel.

    One press only, as SomeCardsConfirmView: both buttons check is_finished
    first, so two Confirms in the same tick cannot both offer. A timeout does
    nothing.
    """

    def __init__(self, session_id: str, presser_id: str, is_manager: bool,
                 draft: Any) -> None:
        super().__init__(timeout=180)
        self.session_id, self.presser_id = session_id, presser_id
        self.is_manager, self.draft = is_manager, draft

        team_a, team_b = _ids(draft.team_a), _ids(draft.team_b)
        seated = (team_a if presser_id in team_a
                  else team_b if presser_id in team_b else team_a + team_b)
        sign_ups = {str(k): v for k, v in (draft.sign_ups or {}).items()}
        self.absent_select: "discord.ui.Select[Any]" = discord.ui.Select(
            placeholder="Who can't play?", custom_id="library_sub:absent",
            min_values=1, max_values=1,
            options=[discord.SelectOption(label=str(sign_ups.get(p) or f"<@{p}>"), value=p)
                     for p in seated])
        self.sub_select = PrefilledSelect(
            discord.ComponentType.user_select, [], placeholder="Who is subbing?",
            min_values=1, max_values=1, custom_id="library_sub:who")
        self.absent_select.callback = _acknowledge
        self.sub_select.callback = _acknowledge

        # The selects go above the buttons the decorators already added.
        buttons = list(self.children)
        self.clear_items()
        for item in (self.absent_select, self.sub_select, *buttons):
            self.add_item(item)

    @classmethod
    async def build(cls, session_id: str, presser_id: str,
                    is_manager: bool) -> "SubPickerView":
        async with AsyncSessionLocal() as session:
            draft = await session.scalar(
                select(DraftSession).where(DraftSession.session_id == str(session_id)))
        if draft is None:
            raise ValueError(f"no draft {session_id}")
        return cls(str(session_id), str(presser_id), is_manager, draft)

    def _picks(self) -> "Optional[tuple[str, Any]]":
        absent, sub = self.absent_select.values, self.sub_select.values
        if not absent or not sub:
            return None
        return str(absent[0]), sub[0]

    @ui_button(label="Confirm", style=discord.ButtonStyle.primary)
    async def confirm(self, button: "discord.ui.Button[Any]",
                      interaction: discord.Interaction) -> None:
        if self.is_finished():
            return
        picks = self._picks()
        if picks is None:
            await interaction.response.send_message("Pick both first.", ephemeral=True)
            return
        self.stop()
        # A component defer updates the picker's own message, so the picker
        # is this press's @original and the replies are followups.
        await interaction.response.defer()
        try:
            await self._confirmed(interaction, *picks)
        except Exception:
            logger.exception("library sub: Confirm failed in {}", self.session_id)
            await interaction.followup.send(WENT_WRONG, ephemeral=True)
        finally:
            await self._take_picker_away(interaction)

    async def _confirmed(self, interaction: discord.Interaction,
                         absent_id: str, member: Any) -> None:
        if not isinstance(member, discord.Member):
            await interaction.followup.send("📭 Pick a member of this server.",
                                            ephemeral=True)
            return
        presser = not_none(interaction.user)
        presser_name = (get_display_name(presser, interaction.guild)
                        if isinstance(presser, discord.Member) else presser.display_name)
        granted: Optional[bool] = None    # None: no grant was needed
        announced = False                 # the grant's announcement carried the line
        loan_id: Optional[int] = None
        decision: Optional[GrantDecision] = None
        async with _lock_for(self.session_id):
            ctx = await sub_context(self.session_id, absent_id, str(member.id),
                                    member.display_name)
            refused = await check_sub(ctx)
            if refused is None and not ctx.sub_in_draft:
                decision, err = resolve_sub_grant(
                    self.draft, self.presser_id, ctx.sub_id, self.is_manager,
                    team_choice=ctx.team_key)
                if decision is None:
                    refused = f"📭 {err}"
            if refused is None:
                result = await offer_to_sub(ctx)
                if isinstance(result, str):
                    refused = result
                else:
                    loan_id = result
            if refused is None and decision is not None:
                # Only now is the announcement's "has been offered" true. A
                # grant that fails leaves the offer standing.
                grant = await grant_sub_access(
                    interaction.guild, self.draft, member, decision,
                    added_by=presser_name, note=chat_line(ctx))
                granted, announced = bool(grant.granted), grant.announced
        # Outside the lock: the sync takes it.
        await nudge(interaction.client, self.session_id)
        if refused is not None:
            await interaction.followup.send(refused, ephemeral=True)
            return
        if loan_id is not None:
            # Now, not at the watchdog's next tick: the offer lasts 15 minutes.
            await _say_it_is_ready(loan_id, ctx.sub_id)
        await interaction.followup.send(
            confirm_reply(ctx) + (NO_GRANT if granted is False else ""), ephemeral=True)
        if not announced:
            # An insider got no grant announcement, or the grant's own did not
            # reach the draft chat: the line is posted here instead.
            await not_none(interaction.channel).send(chat_line(ctx))

    async def _take_picker_away(self, interaction: discord.Interaction) -> None:
        """Clear the picker, which is this press's @original. A miss only
        leaves dead components behind, so it is logged, never raised over the
        offer."""
        try:
            await interaction.edit_original_response(view=None)
        except discord.HTTPException:
            logger.warning("library sub: could not clear the picker in {}",
                           self.session_id)

    @ui_button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: "discord.ui.Button[Any]",
                     interaction: discord.Interaction) -> None:
        if self.is_finished():
            return
        self.stop()
        await interaction.response.edit_message(content="👌 Nobody subbed.", view=None)
