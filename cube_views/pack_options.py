"""Dependency-light cube/pack selection building blocks.

These live in ``cube_views`` (importing only ``discord``) so that both the
draft-start flow (``modals.py``) and the in-draft Update Cube flow
(``views.py`` via ``CubeSelectionView``) can share the exact same controls
without the circular import that ``modals`` -> ``sessions`` -> ``views`` would
otherwise create.
"""
import discord
from config import get_cube_options
from helpers.utils import not_none

# Default pack structure (standard MTG draft / Draftmancer defaults).
DEFAULT_PACKS_PER_PLAYER = 3
DEFAULT_CARDS_PER_PACK = 15
PACKS_PER_PLAYER_MIN, PACKS_PER_PLAYER_MAX = 1, 15
CARDS_PER_PACK_MIN, CARDS_PER_PACK_MAX = 1, 24


# Embed field used to surface non-default pack structure on the draft post.
PACK_FORMAT_FIELD_NAME = "Pack Format:"


def pack_format_display(packs_per_player, cards_per_pack):
    """Human-readable pack structure, or None when it matches the default.

    Used to decide whether to show a pack-format field on the draft embed —
    standard 3 x 15 drafts return None so the embed stays uncluttered.
    """
    if packs_per_player == DEFAULT_PACKS_PER_PLAYER and cards_per_pack == DEFAULT_CARDS_PER_PACK:
        return None
    return f"{packs_per_player} packs × {cards_per_pack} cards"


def selected_value(interaction: discord.Interaction):
    """Pull the chosen string-select value from the raw interaction payload."""
    raw_data: dict = interaction.data or {}  # pyrefly: ignore
    values = raw_data.get("values") or []
    return values[0] if values else None


def parse_pack_settings(packs_raw, cards_raw):
    """Parse and validate the advanced-options inputs.

    Returns (packs_per_player, cards_per_pack, errors). A field that fails to
    parse/validate comes back as None with a human-readable error appended.
    """
    errors = []

    def _parse(raw, name, lo, hi):
        try:
            value = int(str(raw).strip())
        except (ValueError, TypeError):
            errors.append(f"{name} must be a whole number.")
            return None
        if not (lo <= value <= hi):
            errors.append(f"{name} must be between {lo} and {hi}.")
            return None
        return value

    packs = _parse(packs_raw, "Packs per player", PACKS_PER_PLAYER_MIN, PACKS_PER_PLAYER_MAX)
    cards = _parse(cards_raw, "Cards per pack", CARDS_PER_PACK_MIN, CARDS_PER_PACK_MAX)
    return packs, cards, errors


class AdvancedOptionsModal(discord.ui.Modal):
    """Optional per-pod overrides for pack structure. Stores results on a view."""

    def __init__(self, view, *args, **kwargs):
        super().__init__(title="Advanced Draft Options", *args, **kwargs)
        self.view_ref = view
        self.add_item(discord.ui.InputText(
            label=f"Packs per player ({PACKS_PER_PLAYER_MIN}-{PACKS_PER_PLAYER_MAX})",
            value=str(view.packs_per_player),
            custom_id="packs_per_player_input",
        ))
        self.add_item(discord.ui.InputText(
            label=f"Cards per pack ({CARDS_PER_PACK_MIN}-{CARDS_PER_PACK_MAX})",
            value=str(view.cards_per_pack),
            custom_id="cards_per_pack_input",
        ))

    async def callback(self, interaction: discord.Interaction) -> None:
        packs, cards, errors = parse_pack_settings(
            self.children[0].value, self.children[1].value
        )
        if errors:
            await interaction.response.send_message(
                "❌ Could not save advanced options:\n" + "\n".join(errors),
                ephemeral=True,
            )
            return

        self.view_ref.packs_per_player = not_none(packs)
        self.view_ref.cards_per_pack = not_none(cards)
        await interaction.response.send_message(
            f"✅ Advanced options set: **{packs}** packs per player, **{cards}** cards per pack.\n"
            "Select a cube (if you haven't) and click the green ✅ button when ready.",
            ephemeral=True,
        )


class CustomCubeNameModal(discord.ui.Modal):
    """Collects a custom cube id, then runs an async submit handler.

    Used when the user picks "Custom Cube..." and then submits — mirrors how the
    draft-start flow collects a custom cube name.
    """

    def __init__(self, view, on_submit, *args, **kwargs):
        super().__init__(title="Custom Cube", *args, **kwargs)
        self.view_ref = view
        self.on_submit = on_submit
        self.add_item(discord.ui.InputText(
            label="Custom Cube Name",
            placeholder="Enter your cube name",
            custom_id="cube_name_input",
        ))

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view_ref.cube_choice = self.children[0].value
        await self.on_submit(interaction, self.view_ref)


class BaseCubeSelectionView(discord.ui.View):
    """Shared cube-selection UI used by both the draft-start and Update Cube flows.

    Provides the cube dropdown (with a Custom Cube option), an Advanced Options
    button for pack settings, and a submit button. Subclasses implement
    ``submit_callback`` and may override ``submit_label`` / ``submit_emoji``.
    """

    submit_label = "Start Draft"
    submit_emoji = "✅"

    def __init__(self, session_type, guild_id, current_cube=None):
        super().__init__()
        self.session_type = session_type
        self.cube_choice = current_cube
        self.packs_per_player = DEFAULT_PACKS_PER_PLAYER
        self.cards_per_pack = DEFAULT_CARDS_PER_PACK

        options = [discord.SelectOption(**opt) for opt in get_cube_options(guild_id, session_type)]  # pyrefly: ignore
        options.append(discord.SelectOption(label="Custom Cube...", value="custom"))
        self.cube_select = discord.ui.Select(placeholder="Select a Cube", options=options)
        self.cube_select.callback = self.cube_select_callback
        self.add_item(self.cube_select)

        self.advanced_button = discord.ui.Button(
            label="Advanced Options", emoji="⚙️", style=discord.ButtonStyle.secondary
        )
        self.advanced_button.callback = self.advanced_options_callback
        self.add_item(self.advanced_button)

        self.submit_button = discord.ui.Button(
            label=self.submit_label, emoji=self.submit_emoji, style=discord.ButtonStyle.success
        )
        self.submit_button.callback = self.submit_callback
        self.add_item(self.submit_button)

    async def advanced_options_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AdvancedOptionsModal(self))

    async def cube_select_callback(self, interaction: discord.Interaction):
        self.cube_choice = selected_value(interaction)
        # Keep the chosen option highlighted after the message is re-rendered.
        for opt in self.cube_select.options:
            opt.default = (opt.value == self.cube_choice)
        label = next(
            (o.label for o in self.cube_select.options if o.value == self.cube_choice),
            self.cube_choice,
        )
        await interaction.response.edit_message(
            content=(
                f"✅ Cube selected: **{label}**.\n"
                f"Optionally adjust ⚙️ **Advanced Options**, then click {self.submit_emoji} "
                f"**{self.submit_label}** when ready."
            ),
            view=self,
        )

    async def submit_callback(self, interaction: discord.Interaction):
        raise NotImplementedError
