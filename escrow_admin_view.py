"""The moderator's escrow panel: browse open escrows, then release or return one.

Top-level module (like wallet_history_view.py) so the escrow cog can import it without
a cycle through cogs/.

The panel is an ephemeral message only the moderator who ran /escrow-admin list can see
or click, so its buttons carry no author check, and it is not persistent: an ephemeral
message is only editable through the interaction that made it. Nothing the panel draws
is trusted on the next click. A page is built from the database, the select carries only
escrow ids, and the detail and confirm steps look their escrow up again, so a decision
made by another moderator in the meantime shows up as a refusal instead of a stale button.
"""
import math
from typing import Any

import discord
from loguru import logger
from typing_extensions import override

from helpers.display_names import get_member_name_plain
from helpers.escrow_text import NO_MENTIONS, describe_escrow
from helpers.utils import not_none, ui_button
from models.escrow_hold import OPEN, RELEASED, RETURNED, EscrowHold
from services import escrow_service

PAGE_SIZE = 10
TIMEOUT_SECONDS = 300
BUTTON_LABEL_MAX = 80
SELECT_TEXT_MAX = 100


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _name(guild: discord.Guild | None, user_id: str) -> str:
    """A party's display name, or their id if they have left the server."""
    return get_member_name_plain(guild, user_id) if guild else f"User {user_id}"


def _already_decided_text(escrow: EscrowHold, moderator: str) -> str:
    """What happened to a decided escrow, from the point of view of the moderator
    reading it; a double click on Confirm lands on the first form."""
    if escrow.decided_by == moderator:
        return f"You already {escrow.status} escrow #{escrow.id}."
    return f"Escrow #{escrow.id} was already {escrow.status} by <@{escrow.decided_by}>."


class _StepView(discord.ui.View):
    """One screen of the panel. Remembers the page the moderator came from, so every
    way back lands where they were."""

    def __init__(self, guild_id: str, guild: discord.Guild | None, page: int) -> None:
        super().__init__(timeout=TIMEOUT_SECONDS)
        self.guild_id = guild_id
        self.guild = guild
        self.page = page
        # The interaction whose message this view is on screen in. A followup
        # WebhookMessage is only good for the command's own token and a component
        # interaction's message is not editable through the bot token, but
        # edit_original_response on the latest interaction edits the ephemeral
        # message within that interaction's own 15 minutes, which a 5 minute
        # timeout never outlives.
        self.interaction: discord.Interaction | None = None

    async def swap(self, interaction: discord.Interaction, embed: discord.Embed,
                    view: "_StepView", *, deferred: bool = False) -> None:
        """Replace this view's screen with the next step.

        ``deferred`` says the interaction was already acknowledged with ``defer()``
        (a step that has slow work to do first), so the screen is drawn through
        ``edit_original_response``; otherwise the edit is the interaction's own response.

        py-cord does not stop the outgoing view when its message is edited, so its
        timer would run on and later grey out whichever screen is current by then.
        It is stopped only once the edit has landed: if the edit fails the message
        still shows this screen, and the moderator must be able to keep using it.
        """
        try:
            if deferred:
                await interaction.edit_original_response(
                    embed=embed, view=view, allowed_mentions=NO_MENTIONS)
            else:
                await interaction.response.edit_message(
                    embed=embed, view=view, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as e:
            logger.warning(f"escrow panel: could not draw the next screen: {e}")
            return
        self.stop()
        view.interaction = interaction

    @override
    async def on_timeout(self) -> None:
        self.disable_all_items()
        if self.interaction:
            try:
                await self.interaction.edit_original_response(view=self)
            except discord.HTTPException:
                logger.debug("escrow panel: expired message could not be disabled")

    def _add_button(self, label: str, style: discord.ButtonStyle, row: int = 1) -> "discord.ui.Button[Any]":
        button: discord.ui.Button[Any] = discord.ui.Button(
            label=_clip(label, BUTTON_LABEL_MAX), style=style, row=row)
        self.add_item(button)
        return button

    def _add_back_to_list(self, label: str = "Back to list") -> None:
        async def go(interaction: discord.Interaction) -> None:
            await _show_list(self, interaction, self.guild_id, self.guild, self.page)
        self._add_button(label, discord.ButtonStyle.secondary).callback = go


class EscrowPanel(_StepView):
    """One page of open escrows, with a select to open one."""

    def __init__(self, guild_id: str, guild: discord.Guild | None, page: int,
                 pages: int, rows: list[EscrowHold]) -> None:
        super().__init__(guild_id, guild, page)
        self.prev_button.disabled = page <= 0
        self.next_button.disabled = page >= pages - 1
        self.embed = discord.Embed(title="Open escrows", color=discord.Color.gold())
        if rows:
            self.embed.description = "\n".join(describe_escrow(e, age=True) for e in rows)
            self.embed.set_footer(text=f"Page {page + 1} of {pages}")
            self._add_select(rows)
        else:
            self.embed.description = "No open escrows."

    @classmethod
    async def build(cls, guild_id: str, page: int = 0, *,
                    guild: discord.Guild | None = None) -> "EscrowPanel":
        """The panel for `page`, read fresh; a page past the end becomes the last one."""
        escrows = await escrow_service.open_escrows(guild_id)
        pages = max(1, math.ceil(len(escrows) / PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        rows = escrows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        return cls(guild_id, guild, page, pages, rows)

    def _add_select(self, rows: list[EscrowHold]) -> None:
        select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder="Choose an escrow", row=0,
            options=[discord.SelectOption(
                label=_clip(f"#{e.id} · {e.amount} tix · {_name(self.guild, e.funder_id)}"
                            f" → {_name(self.guild, e.counterparty_id)}", SELECT_TEXT_MAX),
                value=str(e.id),
                description=_clip(" ".join(str(e.note).split()), SELECT_TEXT_MAX))
                for e in rows])

        async def chosen(interaction: discord.Interaction) -> None:
            value = select.values[0]
            if not isinstance(value, str):
                raise TypeError(f"expected a string select value, got {type(value).__name__}")
            await _show_detail(self, interaction, self.guild_id, self.guild, self.page, int(value))
        select.callback = chosen
        self.add_item(select)

    @ui_button(label="◀", style=discord.ButtonStyle.blurple, row=1)
    async def prev_button(self, button: "discord.ui.Button[Any]", interaction: discord.Interaction) -> None:
        await _show_list(self, interaction, self.guild_id, self.guild, self.page - 1)

    @ui_button(label="▶", style=discord.ButtonStyle.blurple, row=1)
    async def next_button(self, button: "discord.ui.Button[Any]", interaction: discord.Interaction) -> None:
        await _show_list(self, interaction, self.guild_id, self.guild, self.page + 1)


class _NoticeView(_StepView):
    """A line of text and the way back to the list."""

    def __init__(self, guild_id: str, guild: discord.Guild | None, page: int) -> None:
        super().__init__(guild_id, guild, page)
        self._add_back_to_list()


class _DetailView(_StepView):
    def __init__(self, guild_id: str, guild: discord.Guild | None, page: int,
                 escrow: EscrowHold) -> None:
        super().__init__(guild_id, guild, page)
        escrow_id = int(escrow.id)
        release = self._add_button(f"Release to {_name(guild, escrow.counterparty_id)}",
                                   discord.ButtonStyle.success)
        giveback = self._add_button(f"Return to {_name(guild, escrow.funder_id)}",
                                    discord.ButtonStyle.danger)

        async def ask_release(interaction: discord.Interaction) -> None:
            await _show_confirm(self, interaction, guild_id, guild, page, escrow_id, RELEASED)

        async def ask_return(interaction: discord.Interaction) -> None:
            await _show_confirm(self, interaction, guild_id, guild, page, escrow_id, RETURNED)
        release.callback = ask_release
        giveback.callback = ask_return
        self._add_back_to_list("Back")


class _ConfirmView(_StepView):
    def __init__(self, guild_id: str, guild: discord.Guild | None, page: int,
                 escrow: EscrowHold, outcome: str) -> None:
        super().__init__(guild_id, guild, page)
        escrow_id = int(escrow.id)
        verb = "release" if outcome == RELEASED else "return"
        who = _name(guild, escrow_service.recipient_of(escrow, outcome))
        confirm = self._add_button(f"Confirm: {verb} {escrow.amount} tix to {who}",
                                   discord.ButtonStyle.danger)
        cancel = self._add_button("Cancel", discord.ButtonStyle.secondary)

        async def do_it(interaction: discord.Interaction) -> None:
            await _decide(self, interaction, guild_id, guild, page, escrow_id, outcome)

        async def back(interaction: discord.Interaction) -> None:
            await _show_detail(self, interaction, guild_id, guild, page, escrow_id)
        confirm.callback = do_it
        cancel.callback = back


async def _show_list(via: _StepView, interaction: discord.Interaction, guild_id: str,
                     guild: discord.Guild | None, page: int) -> None:
    panel = await EscrowPanel.build(guild_id, page, guild=guild)
    await via.swap(interaction, panel.embed, panel)


async def _show_notice(via: _StepView, interaction: discord.Interaction, guild_id: str,
                       guild: discord.Guild | None, page: int, text: str, *,
                       deferred: bool = False) -> None:
    await via.swap(interaction, discord.Embed(description=text, color=discord.Color.gold()),
                   _NoticeView(guild_id, guild, page), deferred=deferred)


async def _open_escrow_or_explain(via: _StepView, interaction: discord.Interaction,
                                  guild_id: str, guild: discord.Guild | None, page: int,
                                  escrow_id: int) -> EscrowHold | None:
    """The escrow if it is still open. Otherwise draws the notice saying what became of
    it (another moderator may have decided it since the list was drawn) and returns
    None."""
    escrow = await escrow_service.get_escrow(guild_id, escrow_id)
    if escrow is not None and escrow.status == OPEN:
        return escrow
    if escrow is None:
        text = "That escrow no longer exists."
    else:
        text = _already_decided_text(escrow, str(not_none(interaction.user).id))
    await _show_notice(via, interaction, guild_id, guild, page, text)
    return None


async def _show_detail(via: _StepView, interaction: discord.Interaction, guild_id: str,
                       guild: discord.Guild | None, page: int, escrow_id: int) -> None:
    escrow = await _open_escrow_or_explain(via, interaction, guild_id, guild, page, escrow_id)
    if escrow is None:
        return
    embed = discord.Embed(title=f"Escrow #{escrow_id}", color=discord.Color.gold(),
                          description=describe_escrow(escrow, age=True))
    await via.swap(interaction, embed, _DetailView(guild_id, guild, page, escrow))


async def _show_confirm(via: _StepView, interaction: discord.Interaction, guild_id: str,
                        guild: discord.Guild | None, page: int, escrow_id: int,
                        outcome: str) -> None:
    escrow = await _open_escrow_or_explain(via, interaction, guild_id, guild, page, escrow_id)
    if escrow is None:
        return
    embed = discord.Embed(title=f"Escrow #{escrow_id}", color=discord.Color.red(),
                          description=describe_escrow(escrow, age=True))
    await via.swap(interaction, embed, _ConfirmView(guild_id, guild, page, escrow, outcome))


async def _decide(via: _StepView, interaction: discord.Interaction, guild_id: str,
                  guild: discord.Guild | None, page: int, escrow_id: int,
                  outcome: str) -> None:
    # decide() returns only after the DMs and the recipient's debt settlement, which can
    # outlast the 3 seconds an interaction's first response has. Acknowledge now and
    # draw the outcome afterwards, so a release that succeeded cannot look failed.
    await interaction.response.defer()
    moderator = str(not_none(interaction.user).id)
    try:
        decided = await escrow_service.decide(guild_id, escrow_id, outcome, moderator)
    except escrow_service.EscrowAlreadyDecided as e:
        text = _already_decided_text(e.escrow, moderator)
    except escrow_service.EscrowNotFound:
        text = "That escrow no longer exists."
    else:
        text = (f"Escrow #{escrow_id} {decided.status}: {decided.amount} tix to "
                f"<@{escrow_service.recipient_of(decided, outcome)}>.")
    await _show_notice(via, interaction, guild_id, guild, page, text, deferred=True)
