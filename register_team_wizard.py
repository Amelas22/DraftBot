"""Register a team: the button on the tournament registration board and the private
wizard it opens.

Captains kept getting lost in the board's typed instructions -- link MTGO, register,
deposit, then add_teammate once per player -- so the button walks them through it:

  link MTGO (paid events, only if not linked yet)
  -> Step 1 of 3, the team name
  -> Step 2 of 3, teammates, pre-filled from a full Find a team team
  -> Step 3 of 3, check and confirm.

Nothing is written before the confirm button except the MTGO link. The work itself is
services/tournament_registration, the same code the slash commands run.

A captain who already has a team gets an "add a teammate?" prompt instead.
"""
from dataclasses import dataclass, field
from typing import Any, cast

import discord
from discord.types.components import SelectMenu as SelectMenuPayload
from loguru import logger
from typing_extensions import override

from database.db_session import db_session
from helpers.money_gate import gate_serve, linked_username
from helpers.utils import not_none, ui_button
from models.mtgo_account import MtgoAccount
from models.tournament import Tournament, TournamentParticipant
from notification_service import send_dm
from services import wallet_service
from services.team_finder import full_team_member_ids
from services.team_finder_sync import reconcile_after_command
from services.tournament_registration import (
    RegisterOutcome, RosterAdd, add_roster_members, register_captain)
from services.tournament_service import (
    find_participant_by_name, find_participants_for_captain, get_active_tournament)

WIZARD_TIMEOUT = 600
PRESS_AGAIN = "press **Register a team** again"
MAX_TEAMMATES = 25  # Discord's cap on a select's picks


@dataclass
class WizardState:
    """What the captain has entered so far; nothing here is saved until confirm."""
    guild_id: str
    tournament_id: int
    tournament_name: str
    fee: int
    captain_id: str
    mtgo: str | None = None
    team_name: str = ""
    teammate_ids: list[str] = field(default_factory=list)
    from_find_a_team: bool = False
    picked: bool = False  # the teammate step has been shown once (defaults loaded)
    current: "WizardView | None" = None  # the step on screen; only it may time out


class WizardView(discord.ui.View):
    """A wizard step. When it times out while still on screen, it says how to start
    over instead of leaving buttons that fail with Discord's generic error."""

    def __init__(self, state: WizardState) -> None:
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.state = state
        self.shown_by: discord.Interaction | None = None

    def present(self, interaction: discord.Interaction) -> None:
        """Mark this step as the one on screen, shown through ``interaction``."""
        self.shown_by = interaction
        self.state.current = self

    @override
    async def on_timeout(self) -> None:
        # A later step replaced this one on screen (or on a newer message): leave it.
        if self.shown_by is None or self.state.current is not self:
            return
        try:
            await self.shown_by.edit_original_response(
                content=f"This step timed out — {PRESS_AGAIN}.", view=None)
        except discord.HTTPException:
            pass


def _bot(interaction: discord.Interaction) -> discord.Bot:
    # The bot is the only client that ever dispatches these components.
    return cast(discord.Bot, interaction.client)


async def _reply(interaction: discord.Interaction, content: str,
                 view: discord.ui.View | None = None, *, edit: bool) -> None:
    """Show the next step: in place of the wizard message a button sat on, or as a
    new private message when the step came from the public board (editing there
    would rewrite the board)."""
    if isinstance(view, WizardView):
        view.present(interaction)
    if edit:
        await interaction.response.edit_message(content=content, view=view)
    elif view is None:
        await interaction.response.send_message(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, view=view, ephemeral=True)


def _name_of(guild: discord.Guild | None, user_id: str) -> str:
    member = guild.get_member(int(user_id)) if guild is not None else None
    return member.display_name if member is not None else f"<@{user_id}>"


# --- Entry ------------------------------------------------------------------------

async def start(interaction: discord.Interaction) -> None:
    """The board button: pick the first step for whoever pressed it."""
    guild = not_none(interaction.guild)
    captain_id = str(not_none(interaction.user).id)
    async with db_session() as session:
        tournament = await get_active_tournament(session, guild.id)
        if tournament is None or tournament.status != "registration":
            await interaction.response.send_message(
                "Registration isn't open right now.", ephemeral=True)
            return
        state = WizardState(str(guild.id), tournament.id, tournament.name,
                            tournament.entry_fee or 0, captain_id)
        teams = list(await find_participants_for_captain(session, tournament.id, captain_id))
    if teams:
        await start_existing(interaction, state, teams)
        return
    if state.fee > 0:
        gate = gate_serve(interaction)
        if gate:
            await interaction.response.send_message(f"❌ {gate}", ephemeral=True)
            return
        state.mtgo = await linked_username(captain_id)
        if not state.mtgo:
            await interaction.response.send_modal(LinkModal(state))
            return
    await interaction.response.send_modal(NameModal(state, edit=False))


# --- Linking MTGO -----------------------------------------------------------------

class LinkModal(discord.ui.Modal):
    def __init__(self, state: WizardState) -> None:
        super().__init__(title="Link your MTGO account", timeout=None)
        self.state = state
        self.add_item(discord.ui.InputText(
            label="MTGO username", placeholder="e.g. rook_mtgo", max_length=64))

    @override
    async def callback(self, interaction: discord.Interaction) -> None:
        username = (self.children[0].value or "").strip()
        status, detail = await MtgoAccount.link(int(self.state.captain_id), username,
                                                int(self.state.guild_id))
        if status == "empty":
            await _reply(interaction, f"Enter your MTGO username — {PRESS_AGAIN}.", edit=False)
            return
        if status == "conflict":
            await _reply(interaction,
                         f"That MTGO username is already linked to <@{detail}>. If it's "
                         f"really yours, ask an admin to reassign it with `/link_mtgo_for`.",
                         edit=False)
            return
        self.state.mtgo = str(detail)
        await _reply(interaction, f"✓ Linked as {detail}. Next, name your team.",
                     NameButtonView(self.state, "Name your team"), edit=False)


class NameButtonView(WizardView):
    """One button that opens the team-name pop-up: after linking, and after a
    name turned out to be taken."""

    def __init__(self, state: WizardState, label: str) -> None:
        super().__init__(state)
        self.name_button.label = label

    @ui_button(label="Name your team", style=discord.ButtonStyle.primary)
    async def name_button(self, _button: discord.ui.Button[Any],
                          interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(NameModal(self.state, edit=True))


# --- Step 1: the name -------------------------------------------------------------

class NameModal(discord.ui.Modal):
    def __init__(self, state: WizardState, *, edit: bool) -> None:
        super().__init__(title="Register a team", timeout=None)
        self.state = state
        self.edit = edit
        self.add_item(discord.ui.InputText(
            label="Team name", value=state.team_name or None, max_length=100))

    @override
    async def callback(self, interaction: discord.Interaction) -> None:
        name = (self.children[0].value or "").strip()
        if not name:
            await _reply(interaction, f"Your team needs a name — {PRESS_AGAIN}.", edit=self.edit)
            return
        self.state.team_name = name
        if await _name_taken(self.state):
            await _reply(interaction, _taken_text(self.state),
                         NameButtonView(self.state, "Rename"), edit=self.edit)
            return
        await _show_teammates(interaction, self.state, edit=self.edit)


async def _name_taken(state: WizardState) -> bool:
    async with db_session() as session:
        return await find_participant_by_name(
            session, state.tournament_id, state.team_name) is not None


def _taken_text(state: WizardState) -> str:
    return (f"**{state.team_name}** is already registered for **{state.tournament_name}** "
            f"— pick another name.")


# --- Step 2: teammates ------------------------------------------------------------

async def lfg_defaults(guild_id: str, captain_id: str) -> list[str]:
    async with db_session() as session:
        return await full_team_member_ids(session, guild_id, captain_id)


class PrefilledUserSelect(discord.ui.Select[Any]):
    """A member picker that can open with members already chosen.

    py-cord 2.6.1 has no ``default_values``, so it is added to the component payload
    here; Discord renders those members as picked."""

    def __init__(self, defaults: list[str], **kwargs: Any) -> None:
        super().__init__(discord.ComponentType.user_select, **kwargs)
        self.defaults = defaults

    @override
    def to_component_dict(self) -> SelectMenuPayload:
        payload = super().to_component_dict()
        if self.defaults:
            # The payload TypedDict predates default_values; Discord accepts the key.
            cast(dict[str, Any], payload)["default_values"] = [
                {"id": uid, "type": "user"} for uid in self.defaults]
        return payload


def _pick(values: list[Any], captain_id: str) -> tuple[list[str], list[str]]:
    """(teammate ids, names dropped): bots and the captain can't be teammates."""
    kept: list[str] = []
    dropped: list[str] = []
    for user in values:
        if getattr(user, "bot", False):
            dropped.append(user.display_name)
        elif str(user.id) != captain_id:
            kept.append(str(user.id))
    return kept, dropped


class TeammatesView(WizardView):
    """Step 2 for a new team (Next / Back), or the add-a-teammate prompt for an
    existing one (Add / Done) when ``participant_id`` is set."""

    def __init__(self, state: WizardState, *, participant_id: int | None = None,
                 header: str = "") -> None:
        super().__init__(state)
        self.participant_id = participant_id
        self.header = header
        picker = PrefilledUserSelect(
            state.teammate_ids, placeholder="Pick your teammates",
            min_values=0, max_values=MAX_TEAMMATES)
        picker.callback = self.on_pick
        self.picker = picker
        self.add_item(picker)
        labels = (("Add", "Done") if participant_id is not None else ("Next", "Back"))
        for label, style, handler in ((labels[0], discord.ButtonStyle.primary, self.on_forward),
                                      (labels[1], discord.ButtonStyle.secondary, self.on_back)):
            button: discord.ui.Button[Any] = discord.ui.Button(label=label, style=style)
            button.callback = handler
            self.add_item(button)

    def content(self, note: str = "") -> str:
        if self.participant_id is not None:
            text = self.header
        else:
            text = (f"**Step 2 of 3 · {self.state.team_name}**\n"
                    f"Who's on your team? Pick everyone except yourself. You can add "
                    f"more later.")
            if self.state.from_find_a_team:
                text += "\n_Filled in from your Find a team team. Change it if your plans changed._"
        return text + (f"\n{note}" if note else "")

    async def on_pick(self, interaction: discord.Interaction) -> None:
        kept, dropped = _pick(list(self.picker.values), self.state.captain_id)
        self.state.teammate_ids = kept
        # The picks are the captain's own now, not what Find a team filled in.
        self.state.from_find_a_team = False
        note = (f"Left out {', '.join(dropped)}: bots can't be on a team." if dropped else "")
        view = TeammatesView(self.state, participant_id=self.participant_id, header=self.header)
        await _reply(interaction, view.content(note), view, edit=True)

    async def on_forward(self, interaction: discord.Interaction) -> None:
        if self.participant_id is not None:
            await _add_to_existing(interaction, self.state, self.participant_id)
        else:
            await _show_confirm(interaction, self.state)

    async def on_back(self, interaction: discord.Interaction) -> None:
        if self.participant_id is not None:
            await interaction.response.edit_message(content="All set.", view=None)
        else:
            await interaction.response.send_modal(NameModal(self.state, edit=True))


async def _show_teammates(interaction: discord.Interaction, state: WizardState, *,
                          edit: bool) -> None:
    if not state.picked:
        state.picked = True
        state.teammate_ids = await lfg_defaults(state.guild_id, state.captain_id)
        state.from_find_a_team = bool(state.teammate_ids)
    view = TeammatesView(state)
    await _reply(interaction, view.content(), view, edit=edit)


# --- Step 3: check and confirm ----------------------------------------------------

async def _confirm_text(state: WizardState, guild: discord.Guild | None) -> tuple[str, bool]:
    """The confirm step's text, and whether the wallet is short of the fee."""
    teammates = ", ".join(_name_of(guild, uid) for uid in state.teammate_ids) or "None yet"
    short = False
    rows = [f"**Step 3 of 3 · Check and confirm**",
            f"**Team:** {state.team_name}",
            f"**Captain:** {_name_of(guild, state.captain_id)} (you)",
            f"**Teammates:** {teammates}"]
    if state.fee > 0:
        balance = await wallet_service.get_balance(state.guild_id, state.captain_id)
        rows.append(f"**MTGO:** {state.mtgo} ✓ linked")
        if balance >= state.fee:
            rows.append(f"**Entry fee:** {state.fee} tix from your wallet "
                        f"(you have {balance}, leaving {balance - state.fee})")
        else:
            short = True
            rows.append(f"**Entry fee:** {state.fee} tix. You have {balance}, so your spot "
                        f"is held until you deposit {state.fee - balance} more.")
    else:
        rows.append("**Entry fee:** free")
    return "\n".join(rows), short


class ConfirmView(WizardView):
    def __init__(self, state: WizardState, *, short: bool = False) -> None:
        super().__init__(state)
        self.busy = False
        # A short wallet holds the spot without charging anything yet; "pay" would
        # promise a charge that isn't going to happen.
        if short:
            label = "Register and hold my spot"
        elif state.fee > 0:
            label = f"Register and pay {state.fee} tix"
        else:
            label = "Register"
        confirm: discord.ui.Button[Any] = discord.ui.Button(
            label=label, style=discord.ButtonStyle.success)
        confirm.callback = self.on_confirm
        back: discord.ui.Button[Any] = discord.ui.Button(label="Back")
        back.callback = self.on_back
        cancel: discord.ui.Button[Any] = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.danger)
        cancel.callback = self.on_cancel
        for item in (confirm, back, cancel):
            self.add_item(item)

    async def on_confirm(self, interaction: discord.Interaction) -> None:
        # Answer first and take the buttons away: registering, the roster and the DMs
        # take seconds, and a second press in that window used to come back "name
        # taken" -- inviting a rename into a second, separately paid team.
        if self.busy:
            await interaction.response.defer()
            return
        self.busy = True
        self.stop()
        await interaction.response.edit_message(content="Registering…", view=None)
        try:
            content, view = await _register(interaction, self.state)
        except Exception:
            logger.exception(f"register wizard: confirm failed for {self.state}")
            content, view = (f"Something went wrong while registering **{self.state.team_name}**. "
                             f"Check the board: if your team is on it, you're registered. "
                             f"If not, {PRESS_AGAIN}."), None
        if isinstance(view, WizardView):
            view.present(interaction)
        await interaction.edit_original_response(content=content, view=view)

    async def on_back(self, interaction: discord.Interaction) -> None:
        await _show_teammates(interaction, self.state, edit=True)

    async def on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="Cancelled — nothing was registered.", view=None)


async def _show_confirm(interaction: discord.Interaction, state: WizardState) -> None:
    text, short = await _confirm_text(state, interaction.guild)
    await _reply(interaction, text, ConfirmView(state, short=short), edit=True)


async def _register(interaction: discord.Interaction,
                    state: WizardState) -> tuple[str, discord.ui.View | None]:
    """Re-check what may have changed since step 1, then register and fill the roster."""
    async with db_session() as session:
        tournament = await session.get(Tournament, state.tournament_id)
        still_open = tournament is not None and tournament.status == "registration"
    if not still_open:
        return f"Registration for **{state.tournament_name}** has closed.", None
    if await _name_taken(state):
        return _taken_text(state), NameButtonView(state, "Rename")
    if state.fee > 0:
        gate = gate_serve(interaction)
        if gate:
            return f"❌ {gate}", None
        if not await linked_username(state.captain_id):
            return f"Your MTGO link is gone — {PRESS_AGAIN} to link it.", None

    out = await register_captain(_bot(interaction), state.guild_id, state.tournament_id,
                                 state.captain_id, state.team_name)
    if out.kind == "refused":
        return f"❌ {out.error}", None
    if out.kind in ("free_exists", "already_paid", "not_captain"):
        # Someone registered this name between the check above and now.
        return _taken_text(state), NameButtonView(state, "Rename")

    roster = await _add_and_tell(interaction, state, not_none(out.participant_id),
                                 out.team_name)
    # Only now: the Find a team reconcile tells each teammate whether they're on the
    # registered roster, so it must see the roster filled.
    await reconcile_after_command(_bot(interaction), state.guild_id)
    return _result_text(out) + roster, None


def _result_text(out: RegisterOutcome) -> str:
    team, event = f"**{out.team_name}**", f"**{out.tournament_name}**"
    if out.kind == "paid":
        return (f"✅ You're in. {team} is registered for {event} and {out.fee} tix went "
                f"into the prize pool.")
    if out.kind == "pending":
        return (f"Spot held. {team} is on the board as pending. You have {out.available} "
                f"tix and the fee is {out.fee}, so run `/wallet deposit {out.deficit}` "
                f"whenever you're ready. Your team is confirmed as soon as the tix arrive; "
                f"you don't need to register again.")
    if out.kind == "hold_failed":
        return (f"⚠️ {team} is registered, but I couldn't hold the entry fee: {out.error}. "
                f"{PRESS_AGAIN.capitalize()}.")
    return f"✅ You're in. {team} is registered for {event}."


async def _add_and_tell(interaction: discord.Interaction, state: WizardState,
                        participant_id: int, team_name: str) -> str:
    """Put the picked members on the roster and DM each newly added one. Returns the
    sentence for the captain's reply."""
    teammate_ids = state.teammate_ids
    if not teammate_ids:
        return f"\nAdd teammates any time: {PRESS_AGAIN}."
    guild = not_none(interaction.guild)
    members: list[discord.Member] = []
    missing: list[str] = []
    for uid in teammate_ids:
        member = guild.get_member(int(uid))
        if member is None:
            try:
                member = await guild.fetch_member(int(uid))
            except discord.HTTPException:
                missing.append(f"<@{uid}>")
                continue
        members.append(member)
    try:
        adds = await add_roster_members(_bot(interaction), guild, participant_id, members)
    except ValueError as e:
        return f"\n❌ Couldn't add teammates: {e}"
    text = await _tell_added(interaction, state, team_name, adds)
    if missing:
        text += f"\nCouldn't find {', '.join(missing)} in this server, so they weren't added."
    return text


async def _tell_added(interaction: discord.Interaction, state: WizardState, team_name: str,
                      adds: list[RosterAdd]) -> str:
    guild = not_none(interaction.guild)
    captain = _name_of(guild, state.captain_id)
    told: list[str] = []
    untold: list[str] = []
    for add in adds:
        name = _name_of(guild, add.user_id)
        if not add.created:
            continue
        message = (f"{captain} added you to **{team_name}** for **{state.tournament_name}** "
                   f"in {guild.name}. If that's a mistake, ask them or a mod to remove you.")
        (told if await send_dm(_bot(interaction), add.user_id, message) else untold).append(name)
    shared = [f"{_name_of(guild, a.user_id)} is also on {', '.join(a.also_on)}"
              for a in adds if a.also_on]
    unroled = [_name_of(guild, a.user_id) for a in adds if not a.role_synced]
    lines = []
    if told:
        lines.append(f"{', '.join(told)} {'has' if len(told) == 1 else 'have'} been told.")
    if untold:
        lines.append(f"I couldn't DM {', '.join(untold)}, so let them know yourself.")
    if shared:
        lines.append("; ".join(shared) + " in this tournament.")
    if unroled:
        lines.append(f"⚠️ Couldn't give {', '.join(unroled)} the **{team_name}** role — "
                     f"check that the bot's own role sits above it.")
    return "".join(f"\n{line}" for line in lines)


# --- A captain who already has a team ---------------------------------------------

async def start_existing(interaction: discord.Interaction, state: WizardState,
                         teams: list[TournamentParticipant]) -> None:
    """Offer to add teammates; a captain of several teams picks one first."""
    if len(teams) == 1:
        await _existing_prompt(interaction, state, teams[0], edit=False)
        return
    await _reply(interaction,
                 f"You captain {len(teams)} teams in **{state.tournament_name}**. Which one?",
                 WhichTeamView(state, teams), edit=False)


class WhichTeamView(WizardView):
    def __init__(self, state: WizardState, teams: list[TournamentParticipant]) -> None:
        super().__init__(state)
        self.teams = {str(p.id): p for p in teams}
        select: discord.ui.Select[Any] = discord.ui.Select(
            placeholder="Pick a team",
            options=[discord.SelectOption(label=p.team_name[:100], value=str(p.id))
                     for p in teams[:25]])
        select.callback = self.on_pick
        self.select = select
        self.add_item(select)

    async def on_pick(self, interaction: discord.Interaction) -> None:
        participant = self.teams[str(self.select.values[0])]
        await _existing_prompt(interaction, self.state, participant, edit=True)


async def _existing_prompt(interaction: discord.Interaction, state: WizardState,
                           participant: TournamentParticipant, *, edit: bool) -> None:
    state.team_name = participant.team_name
    state.teammate_ids = []
    header = (f"You captain **{participant.team_name}** in **{state.tournament_name}**. "
              f"Add a teammate?")
    if state.fee > 0 and participant.status != "paid":
        balance = await wallet_service.get_balance(state.guild_id, state.captain_id)
        deficit = state.fee - balance
        if deficit > 0:
            header += (f"\nPayment pending: deposit {deficit} tix with "
                       f"`/wallet deposit {deficit}`.")
    view = TeammatesView(state, participant_id=participant.id, header=header)
    await _reply(interaction, view.content(), view, edit=edit)


async def _add_to_existing(interaction: discord.Interaction, state: WizardState,
                           participant_id: int) -> None:
    if not state.teammate_ids:
        await interaction.response.edit_message(content="Nobody picked — nothing changed.",
                                                view=None)
        return
    await interaction.response.edit_message(content="Adding…", view=None)
    roster = await _add_and_tell(interaction, state, participant_id, state.team_name)
    await reconcile_after_command(_bot(interaction), state.guild_id)
    logger.info(f"register wizard: {state.captain_id} added {state.teammate_ids} to "
                f"participant {participant_id}")
    await interaction.edit_original_response(
        content=f"✅ Added to **{state.team_name}**.{roster}", view=None)


# --- The board button -------------------------------------------------------------

class RegisterTeamView(discord.ui.View):
    """The button on the registration board. Persistent, one global id: the press
    resolves whichever tournament in the presser's server is taking registrations."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @ui_button(label="Register a team", style=discord.ButtonStyle.success,
               custom_id="tournament:register")
    async def register_button(self, _button: discord.ui.Button[Any],
                              interaction: discord.Interaction) -> None:
        await start(interaction)
