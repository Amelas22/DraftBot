"""The /settings message: an embed of a player's settings and a button for each.

Each click flips the stored value (player_settings.flip), reloads, and redraws
this same message. Row 0 holds the Drafting buttons: the stake cap, offered
only on a money server, and the card library, offered only where the player
may borrow. A new cap, or the library switched on, is carried into the
player's live sign-ups.
"""
import asyncio
from typing import Any

import discord
from loguru import logger
from typing_extensions import override

from helpers.utils import not_none, ui_button
from player_settings import (Setting, Settings, apply_cap_to_live_drafts,
                             apply_library_to_live_drafts, flip, live_result_text,
                             load_settings)
from utils import redraw_leaderboard

CAP_LABEL = "Cap my stake to the other side"
LIBRARY_LABEL = "Offer me a library deck"
LIBRARY_NOT_SET = "○ Not set (you'll be asked at sign-up)"
DMS_LABEL = "DM me when a ready check starts"
HIDE_LABEL = "Hide me from the Skill Rating Ladder"
ANONYMOUS_LABEL = "Show me as Anonymous on donation pages"

NOT_YOURS = "These buttons are not for you."
SAVE_FAILED = "⚠️ Couldn't save that just now. Try again."
EXPIRED = "These buttons have expired. Run `/settings` again."
LIVE_FAILED = ("⚠️ Saved, but I couldn't update the drafts you're signed up for. "
               "Use each draft's **Change Entry / Settings** button.")

# asyncio keeps only a weak reference to a task; hold the ladder redraws here
# until they finish so one is not collected mid-flight.
_background: "set[asyncio.Task[None]]" = set()


def _line(on: bool, label: str) -> str:
    return f"● On  {label}" if on else f"○ Off  {label}"


def _groups(settings: Settings) -> list[tuple[str, list[str]]]:
    """Each embed field: its group name and its lines. Empty groups are left out."""
    drafting: list[str] = []
    if settings.stake_cap is not None:
        drafting.append(_line(settings.stake_cap, CAP_LABEL))
    if settings.library_offered:
        drafting.append(f"{LIBRARY_NOT_SET}  {LIBRARY_LABEL}" if settings.library is None
                        else _line(settings.library, LIBRARY_LABEL))
    return [
        (name, lines) for name, lines in (
            ("Drafting", drafting),
            ("Notifications", [_line(settings.dms, DMS_LABEL)]),
            ("Privacy", [_line(settings.hide_from_ladder, HIDE_LABEL),
                         _line(settings.anonymous, ANONYMOUS_LABEL)]),
        ) if lines
    ]


def settings_embed(guild_name: str, settings: Settings) -> discord.Embed:
    embed = discord.Embed(title=f"⚙️ Your settings: {guild_name}",
                          color=discord.Color.blurple())
    for name, lines in _groups(settings):
        embed.add_field(name=name, value="\n".join(lines), inline=False)
    embed.set_footer(text="Settings are per server.")
    return embed


def _style(on: bool) -> discord.ButtonStyle:
    return discord.ButtonStyle.success if on else discord.ButtonStyle.secondary


class SettingsView(discord.ui.View):
    def __init__(self, player_id: str, guild_id: str, guild_name: str,
                 settings: Settings) -> None:
        super().__init__(timeout=600)
        self.player_id = player_id
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.settings = settings
        # The interaction that opened this message, set by the command, so a
        # timeout can edit the ephemeral message it sent.
        self.opener: discord.Interaction | None = None
        if settings.stake_cap is None:
            self.remove_item(self.cap_button)
        if not settings.library_offered:
            self.remove_item(self.library_button)
        self._restyle()

    def _restyle(self) -> None:
        self.cap_button.style = _style(bool(self.settings.stake_cap))
        self.library_button.style = _style(bool(self.settings.library))
        self.dms_button.style = _style(self.settings.dms)
        self.hide_button.style = _style(self.settings.hide_from_ladder)
        self.anonymous_button.style = _style(self.settings.anonymous)

    async def _toggle(self, interaction: discord.Interaction, setting: Setting) -> None:
        if str(not_none(interaction.user).id) != self.player_id:
            await interaction.response.send_message(NOT_YOURS, ephemeral=True)
            return
        await interaction.response.defer()
        new = await flip(self.player_id, self.guild_id, setting)
        if new is None:
            await interaction.followup.send(SAVE_FAILED, ephemeral=True)
            return
        # Side effects first, so a failed edit of this message can't skip them.
        if setting is Setting.HIDE_FROM_LADDER:
            task = asyncio.create_task(
                redraw_leaderboard(interaction.client, self.guild_id, "sr_ladder"))
            _background.add(task)
            task.add_done_callback(_background.discard)
        # Each follow-up: its text and the view to send with it, if any. The
        # preference is saved by now, so a failure here must not escape.
        live: list[tuple[str, discord.ui.View | None]] = []
        try:
            if setting is Setting.STAKE_CAP:
                text = live_result_text(await apply_cap_to_live_drafts(
                    interaction.client, self.player_id, self.guild_id, new))
                if text:
                    live.append((text, None))
            elif setting is Setting.LIBRARY and new:
                live.extend((text, view) for _draft, text, view
                            in await apply_library_to_live_drafts(self.player_id, self.guild_id)
                            if text is not None)
        except Exception:
            logger.exception("settings: saved {} for {} but couldn't update their live drafts",
                             setting.value, self.player_id)
            live = [(LIVE_FAILED, None)]
        self.settings = await load_settings(self.player_id, self.guild_id)
        self._restyle()
        await interaction.edit_original_response(
            embed=settings_embed(self.guild_name, self.settings), view=self)
        for text, view in live:
            if view is None:
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.followup.send(text, view=view, ephemeral=True)

    @ui_button(label="Stake cap", row=0)
    async def cap_button(self, _button: discord.ui.Button[Any],
                         interaction: discord.Interaction) -> None:
        await self._toggle(interaction, Setting.STAKE_CAP)

    @ui_button(label="Card library", row=0)
    async def library_button(self, _button: discord.ui.Button[Any],
                             interaction: discord.Interaction) -> None:
        await self._toggle(interaction, Setting.LIBRARY)

    @ui_button(label="Ready-check DMs", row=1)
    async def dms_button(self, _button: discord.ui.Button[Any],
                         interaction: discord.Interaction) -> None:
        await self._toggle(interaction, Setting.DMS)

    @ui_button(label="Hide from ladder", row=2)
    async def hide_button(self, _button: discord.ui.Button[Any],
                          interaction: discord.Interaction) -> None:
        await self._toggle(interaction, Setting.HIDE_FROM_LADDER)

    @ui_button(label="Anonymous donor", row=2)
    async def anonymous_button(self, _button: discord.ui.Button[Any],
                               interaction: discord.Interaction) -> None:
        await self._toggle(interaction, Setting.ANONYMOUS)

    @override
    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True
        if self.opener is None:
            return
        try:
            await self.opener.edit_original_response(content=EXPIRED, view=self)
        except discord.HTTPException:
            pass
