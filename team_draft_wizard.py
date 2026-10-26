"""Set up a team draft: the button on the #set-team-draft board and the private
wizard it opens.

  Step 1 of 3, the first side (team roles and/or players, one picker)
  -> Step 2 of 3, the other side
  -> Step 3 of 3, check the thread name and create.

Nothing is written before Create. Create checks the two sides
(services/team_draft.check_sides), saves the matchup and opens its thread
(team_draft_view.open_team_draft_room) in the channel the board sits in.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, cast

import discord
from discord.types.components import SelectMenu as SelectMenuPayload
from loguru import logger
from typing_extensions import override

from config import scheduler_enabled
from database.db_session import db_session
from helpers.matchup import Side, matchup_name, side_label
from helpers.utils import DISCORD_THREAD_NAME_LIMIT, not_none, ui_button
from services.team_draft import (
    MAX_PICKS, Picks, check_sides, create_team_draft, delete_team_draft)
from team_draft_view import NOT_ENABLED, open_team_draft_room

WIZARD_TIMEOUT = 600
PRESS_AGAIN = "press **Set up a team draft** again"
BOARD_TITLE = "Set up a team draft"
BOARD_TEXT = ("Drafting against another team? Press **Set up a team draft**, pick the "
              "two sides — a team role, players by name, or both — and I'll open a "
              "thread for the matchup that tags everyone, where you can talk it over "
              "and start the draft.")
NOT_A_TEXT_CHANNEL = "Press **Set up a team draft** on the board in #set-team-draft."
COULD_NOT_OPEN = ("I couldn't open a thread for this team draft. Check that I can create "
                  f"threads in this channel, then {PRESS_AGAIN}.")

SideKey = Literal["a", "b"]


def board_embed() -> discord.Embed:
    """The board, as first posted and as reposted by the sticky manager."""
    return discord.Embed(title=BOARD_TITLE, description=BOARD_TEXT,
                         color=discord.Color.green())


@dataclass
class SetupState:
    """What the organiser has picked so far; nothing here is saved until Create."""
    guild_id: str
    channel: discord.TextChannel  # where the board is, so where the thread opens
    created_by: str
    a: Side | None = None
    b: Side | None = None
    name: str = ""  # the organiser's own thread name; "" means the default
    current: "WizardView | None" = None  # the step on screen; only it may time out

    def side(self, key: SideKey) -> Side | None:
        return self.a if key == "a" else self.b

    def default_name(self) -> str:
        return matchup_name(not_none(self.a), not_none(self.b))

    def thread_name(self) -> str:
        return self.name or self.default_name()


class WizardView(discord.ui.View):
    """A wizard step. When it times out while still on screen, it says how to start
    over instead of leaving buttons that fail with Discord's generic error."""

    def __init__(self, state: SetupState) -> None:
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.state = state
        self.shown_by: discord.Interaction | None = None

    def present(self, interaction: discord.Interaction) -> None:
        """Mark this step as the one on screen, shown through ``interaction``."""
        self.shown_by = interaction
        self.state.current = self

    @override
    async def on_timeout(self) -> None:
        if self.shown_by is None or self.state.current is not self:
            return
        try:
            await self.shown_by.edit_original_response(
                content=f"This step timed out — {PRESS_AGAIN}.", view=None)
        except discord.HTTPException:
            pass


async def _reply(interaction: discord.Interaction, content: str,
                 view: discord.ui.View | None = None, *, edit: bool) -> None:
    """Show the next step: in place of the wizard message, or as a new private
    message when the press came from the public board (editing would rewrite it)."""
    if isinstance(view, WizardView):
        view.present(interaction)
    if edit:
        await interaction.response.edit_message(content=content, view=view)
    elif view is None:
        await interaction.response.send_message(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, view=view, ephemeral=True)


# --- Entry ------------------------------------------------------------------------

async def start_setup(interaction: discord.Interaction) -> None:
    """The board button: open Step 1 privately for whoever pressed it."""
    guild = not_none(interaction.guild)
    if not scheduler_enabled(str(guild.id)):
        await interaction.response.send_message(NOT_ENABLED, ephemeral=True)
        return
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(NOT_A_TEXT_CHANNEL, ephemeral=True)
        return
    state = SetupState(str(guild.id), channel, str(not_none(interaction.user).id))
    view = SideView(state, "a")
    await _reply(interaction, view.content(), view, edit=False)


# --- Steps 1 and 2: the sides -----------------------------------------------------

class PrefilledMentionableSelect(discord.ui.Select[Any]):
    """A roles-and-members picker that can open with picks already chosen.

    py-cord 2.6.1 has no ``default_values``, so it is added to the component
    payload here (as register_team_wizard.PrefilledUserSelect does for members);
    each default is ``{"id": ..., "type": "user" | "role"}``."""

    def __init__(self, defaults: list[dict[str, str]], **kwargs: Any) -> None:
        super().__init__(discord.ComponentType.mentionable_select, **kwargs)
        self.defaults = defaults

    @override
    def to_component_dict(self) -> SelectMenuPayload:
        payload = super().to_component_dict()
        if self.defaults:
            # The payload TypedDict predates default_values; Discord accepts the key.
            cast(dict[str, Any], payload)["default_values"] = self.defaults
        return payload


def _defaults(side: Side | None) -> list[dict[str, str]]:
    if side is None:
        return []
    return ([{"id": r, "type": "role"} for r in side.role_ids]
            + [{"id": u, "type": "user"} for u in side.user_ids])


def _side_from(values: list[Any]) -> Side:
    """A side from a mentionable select's picks: Roles, and Members or Users."""
    roles = [v for v in values if isinstance(v, discord.Role)]
    people = [v for v in values if not isinstance(v, discord.Role)]
    return Side(
        label=side_label([r.name for r in roles], [p.display_name for p in people]),
        role_ids=tuple(str(r.id) for r in roles),
        user_ids=tuple(str(p.id) for p in people),
    )


class SideView(WizardView):
    """Step 1 (``key`` "a") or Step 2 ("b"). Picking moves straight on; Next is
    there when the side is already picked (after Back) and stays as it was."""

    def __init__(self, state: SetupState, key: SideKey) -> None:
        super().__init__(state)
        self.key: SideKey = key
        picker = PrefilledMentionableSelect(
            _defaults(state.side(key)), placeholder="Pick team roles or players",
            min_values=1, max_values=MAX_PICKS)
        picker.callback = self.on_pick
        self.picker = picker
        self.add_item(picker)
        buttons: list[tuple[str, discord.ButtonStyle, Callable[..., Any]]] = []
        if state.side(key) is not None:
            buttons.append(("Next", discord.ButtonStyle.primary, self.on_next))
        if key == "b":
            buttons.append(("Back", discord.ButtonStyle.secondary, self.on_back))
        for label, style, handler in buttons:
            button: discord.ui.Button[Any] = discord.ui.Button(label=label, style=style)
            button.callback = handler
            self.add_item(button)

    def content(self) -> str:
        pick = (f"Pick a team role, players by name, or both — up to {MAX_PICKS} "
                f"in all.")
        if self.key == "a":
            return f"**Step 1 of 3 · The first side**\n{pick}"
        return (f"**Step 2 of 3 · The other side**\nWho is "
                f"**{not_none(self.state.a).label}** drafting against? {pick}")

    async def on_pick(self, interaction: discord.Interaction) -> None:
        side = _side_from(list(self.picker.values))
        if self.key == "a":
            self.state.a = side
        else:
            self.state.b = side
        await self.on_next(interaction)

    async def on_next(self, interaction: discord.Interaction) -> None:
        if self.key == "a":
            view = SideView(self.state, "b")
            await _reply(interaction, view.content(), view, edit=True)
        else:
            await _show_confirm(interaction, self.state)

    async def on_back(self, interaction: discord.Interaction) -> None:
        view = SideView(self.state, "a")
        await _reply(interaction, view.content(), view, edit=True)


# --- Step 3: name and create ------------------------------------------------------

def _confirm_text(state: SetupState, note: str = "") -> str:
    a, b = not_none(state.a), not_none(state.b)
    rows = ["**Step 3 of 3 · Check and create**",
            f"**First side:** {a.mentions()}",
            f"**Other side:** {b.mentions()}",
            f"**Thread name:** {state.thread_name()}",
            "Create opens the thread here and tags both sides."]
    return "\n".join(rows) + (f"\n{note}" if note else "")


async def _show_confirm(interaction: discord.Interaction, state: SetupState,
                        note: str = "") -> None:
    await _reply(interaction, _confirm_text(state, note), ConfirmView(state), edit=True)


class NameModal(discord.ui.Modal):
    def __init__(self, state: SetupState) -> None:
        super().__init__(title="Name the team draft", timeout=None)
        self.state = state
        self.add_item(discord.ui.InputText(
            label="Thread name (blank for the default)",
            placeholder=state.default_name()[:DISCORD_THREAD_NAME_LIMIT],
            value=state.name or None,
            max_length=DISCORD_THREAD_NAME_LIMIT, required=False))

    @override
    async def callback(self, interaction: discord.Interaction) -> None:
        self.state.name = (self.children[0].value or "").strip()
        await _show_confirm(interaction, self.state)


def _members_of(guild: discord.Guild) -> Callable[[str], list[str] | None]:
    def members(role_id: str) -> list[str] | None:
        role = guild.get_role(int(role_id))
        return None if role is None else [str(m.id) for m in role.members]
    return members


def _is_bot_in(guild: discord.Guild) -> Callable[[str], bool]:
    def is_bot(user_id: str) -> bool:
        member = guild.get_member(int(user_id))
        return member is not None and member.bot
    return is_bot


def _picks(side: Side) -> Picks:
    return Picks(role_ids=side.role_ids, user_ids=side.user_ids)


class ConfirmView(WizardView):
    def __init__(self, state: SetupState) -> None:
        super().__init__(state)
        self.busy = False
        for label, style, handler in (
                ("Create", discord.ButtonStyle.success, self.on_create),
                ("Rename", discord.ButtonStyle.secondary, self.on_rename),
                ("Back", discord.ButtonStyle.secondary, self.on_back)):
            button: discord.ui.Button[Any] = discord.ui.Button(label=label, style=style)
            button.callback = handler
            self.add_item(button)

    async def on_rename(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(NameModal(self.state))

    async def on_back(self, interaction: discord.Interaction) -> None:
        view = SideView(self.state, "b")
        await _reply(interaction, view.content(), view, edit=True)

    async def on_create(self, interaction: discord.Interaction) -> None:
        if self.busy:
            await interaction.response.defer()
            return
        state = self.state
        if not scheduler_enabled(state.guild_id):
            await interaction.response.edit_message(content=NOT_ENABLED, view=None)
            return
        guild = not_none(interaction.guild)
        refusal = check_sides(_picks(not_none(state.a)), _picks(not_none(state.b)),
                              _members_of(guild), state.guild_id, _is_bot_in(guild))
        if refusal is not None:
            await _show_confirm(interaction, state, f"❌ {refusal}")
            return
        # Answer first and take the buttons away: opening the thread takes a
        # moment, and a second press in that window would make a second matchup.
        self.busy = True
        self.stop()
        await interaction.response.edit_message(content="Opening the thread…", view=None)
        try:
            content = await _create(state)
        except Exception:
            logger.exception(f"team draft wizard: create failed for {state}")
            content = (f"Something went wrong while setting up this team draft. If a "
                       f"thread for it appeared, use that one; if not, {PRESS_AGAIN}.")
        await interaction.edit_original_response(content=content, view=None)


async def _create(state: SetupState) -> str:
    """Save the matchup, open its thread, and say where it is."""
    async with db_session() as session:
        row = await create_team_draft(
            session, state.guild_id, str(state.channel.id), state.thread_name(),
            _picks(not_none(state.a)), _picks(not_none(state.b)),
            state.created_by, datetime.now(timezone.utc))
        team_draft_id = row.id
    thread = await open_team_draft_room(state.channel, team_draft_id)
    if thread is None:
        # A matchup without a thread can never be reached; don't keep it.
        async with db_session() as session:
            await delete_team_draft(session, team_draft_id)
        return COULD_NOT_OPEN
    logger.info(f"team draft wizard: {state.created_by} set up team draft {team_draft_id} "
                f"in thread {thread.id}")
    return f"Your team draft is ready: <#{thread.id}>"


# --- The board button -------------------------------------------------------------

class TeamDraftBoardView(discord.ui.View):
    """The button on the #set-team-draft board. Persistent, one global id."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @ui_button(label="Set up a team draft", style=discord.ButtonStyle.success,
               custom_id="team_draft:setup")
    async def setup_button(self, _button: discord.ui.Button[Any],
                           interaction: discord.Interaction) -> None:
        await start_setup(interaction)
