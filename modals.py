import discord
import random
from datetime import datetime
from loguru import logger
from typing import Optional
from config import get_cube_options
from models.session_details import SessionDetails

from sessions import RandomSession, PremadeSession, SwissSession, BaseSession
from sessions.staked_session import StakedSession
from sessions.winston_session import WinstonSession

# Shared, dependency-light selection building blocks live in cube_views so the
# Update Cube flow (views.py) can reuse the exact same controls without a
# circular import. Re-exported here for the draft-start flow below.
from cube_views.pack_options import (  # noqa: F401
    DEFAULT_PACKS_PER_PLAYER,
    DEFAULT_CARDS_PER_PACK,
    parse_pack_settings,
    AdvancedOptionsModal,
    BaseCubeSelectionView,
)


class CubeDraftModal(discord.ui.Modal):
    @classmethod
    def create_if_needed(cls, session_type: str, cube_choice: str,
                         packs_per_player: int = DEFAULT_PACKS_PER_PLAYER,
                         cards_per_pack: int = DEFAULT_CARDS_PER_PACK,
                         session_details_overrides=None) -> Optional['CubeDraftModal']:
        """Create a modal only if it would have input fields."""
        modal = cls(session_type, cube_choice, packs_per_player, cards_per_pack,
                    session_details_overrides=session_details_overrides)
        return modal if modal.children else None

    def __init__(self, session_type: str, cube_choice: str,
                 packs_per_player: int = DEFAULT_PACKS_PER_PLAYER,
                 cards_per_pack: int = DEFAULT_CARDS_PER_PACK,
                 session_details_overrides=None, *args, **kwargs) -> None:
        super().__init__(title="Draft Setup", *args, **kwargs)
        self.session_type: str = session_type
        self.cube_choice = cube_choice
        self.packs_per_player = packs_per_player
        self.cards_per_pack = cards_per_pack
        self.session_details_overrides = session_details_overrides or {}

        if cube_choice == "custom":
            self.add_item(discord.ui.InputText(
                label="Custom Cube Name",
                placeholder="Enter your cube name",
                custom_id="cube_name_input"
            ))

        # Team names are asked for unless the launcher already provided them
        # (e.g. a tournament pairing pre-naming both teams).
        has_team_names = ("team_a_name" in self.session_details_overrides
                          and "team_b_name" in self.session_details_overrides)
        if self.session_type == "premade" and not has_team_names:
            self.add_item(discord.ui.InputText(
                label="Team A Name", 
                placeholder="Enter Team A Name", 
                custom_id="team_a_input"
            ))
            self.add_item(discord.ui.InputText(
                label="Team B Name", 
                placeholder="Enter Team B Name", 
                custom_id="team_b_input"
            ))

    async def callback(self, interaction: discord.Interaction) -> None:
        session_details: SessionDetails = self.configure_session_details(interaction)
        await handle_draft_session(interaction, self.session_type, session_details)

    def configure_session_details(self, interaction: discord.Interaction) -> SessionDetails:
        """Prepare the session details based on the user input."""
        details: SessionDetails = SessionDetails(interaction)
        
        # If custom cube, get name from input, otherwise use preset choice
        if self.cube_choice == "custom":
            details.cube_choice = self.children[0].value
        else:
            details.cube_choice = self.cube_choice

        details.packs_per_player = self.packs_per_player
        details.cards_per_pack = self.cards_per_pack

        team_inputs = [c for c in self.children if c.custom_id in ("team_a_input", "team_b_input")]
        if self.session_type == "premade" and team_inputs:
            details.team_a_name = team_inputs[0].value or "Team A"
            details.team_b_name = team_inputs[1].value or "Team B"

        for attr, value in self.session_details_overrides.items():
            setattr(details, attr, value)

        return details

class CubeDraftSelectionView(BaseCubeSelectionView):
    def __init__(self, session_type, guild_id, current_cube=None, session_details_overrides=None):
        super().__init__(session_type, guild_id, current_cube=current_cube)
        # Extra SessionDetails attributes set by the launcher (e.g. a tournament
        # pairing pre-naming the teams and linking the match for auto-recording).
        self.session_details_overrides = session_details_overrides or {}

    async def submit_callback(self, interaction: discord.Interaction):
        if not self.cube_choice:
            await interaction.response.send_message(
                "❌ Please select a cube before starting the draft.", ephemeral=True
            )
            return

        if modal := CubeDraftModal.create_if_needed(
            self.session_type, self.cube_choice, self.packs_per_player, self.cards_per_pack,
            session_details_overrides=self.session_details_overrides
        ):
            await interaction.response.send_modal(modal)
        else:
            session_details = SessionDetails(interaction)
            session_details.cube_choice = self.cube_choice
            session_details.packs_per_player = self.packs_per_player
            session_details.cards_per_pack = self.cards_per_pack
            for attr, value in self.session_details_overrides.items():
                setattr(session_details, attr, value)
            await handle_draft_session(interaction, self.session_type, session_details)

# Shared utility functions
def create_session_instance(session_type: str, session_details: SessionDetails) -> BaseSession:
    """Dynamically create the appropriate session instance based on the session type."""
    session_class: type = {
        "premade": PremadeSession,
        "swiss": SwissSession,
        'random': RandomSession,
        "winston": WinstonSession
    }.get(session_type, BaseSession)

    return session_class(session_details)

async def handle_draft_session(interaction: discord.Interaction, session_type: str, session_details: SessionDetails) -> None:
    """Handle immediate draft sessions."""
    session_instance = create_session_instance(session_type, session_details)
    await session_instance.create_draft_session(interaction, interaction.client)


class StakedCubeDraftSelectionView(BaseCubeSelectionView):
    def __init__(self, guild_id: int):
        super().__init__("default", guild_id)

    async def submit_callback(self, interaction: discord.Interaction):
        if not self.cube_choice:
            await interaction.response.send_message(
                "❌ Please select a cube before starting the draft.", ephemeral=True
            )
            return

        if modal := StakedCubeDraftModal.create_if_needed(
            self.cube_choice, self.packs_per_player, self.cards_per_pack
        ):
            await interaction.response.send_modal(modal)
        else:
            session_details = SessionDetails(interaction)
            session_details.cube_choice = self.cube_choice
            session_details.min_stake = 20  # Set fixed min stake to 20
            session_details.packs_per_player = self.packs_per_player
            session_details.cards_per_pack = self.cards_per_pack
            await handle_staked_draft_session(interaction, session_details)


class StakedCubeDraftModal(discord.ui.Modal):
    @classmethod
    def create_if_needed(cls, cube_choice: str,
                         packs_per_player: int = DEFAULT_PACKS_PER_PLAYER,
                         cards_per_pack: int = DEFAULT_CARDS_PER_PACK) -> Optional['StakedCubeDraftModal']:
        """Create a modal only if it would have input fields."""
        modal = cls(cube_choice, packs_per_player, cards_per_pack)
        return modal if modal.children else None

    def __init__(self, cube_choice: str,
                 packs_per_player: int = DEFAULT_PACKS_PER_PLAYER,
                 cards_per_pack: int = DEFAULT_CARDS_PER_PACK, *args, **kwargs) -> None:
        super().__init__(title="Staked Draft Setup", *args, **kwargs)
        self.cube_choice = cube_choice
        self.packs_per_player = packs_per_player
        self.cards_per_pack = cards_per_pack

        if cube_choice == "custom":
            self.add_item(discord.ui.InputText(
                label="Custom Cube Name",
                placeholder="Enter your cube name",
                custom_id="cube_name_input"
            ))
            
    async def callback(self, interaction: discord.Interaction) -> None:
        session_details = self.configure_session_details(interaction)
        await handle_staked_draft_session(interaction, session_details)

    def configure_session_details(self, interaction: discord.Interaction) -> SessionDetails:
        """Prepare the session details based on the user input."""
        session_details = SessionDetails(interaction)
        
        # If custom cube, get name from input, otherwise use preset choice
        if self.cube_choice == "custom":
            session_details.cube_choice = self.children[0].value
        else:
            session_details.cube_choice = self.cube_choice

        # Set fixed min stake to 20
        session_details.min_stake = 20
        session_details.packs_per_player = self.packs_per_player
        session_details.cards_per_pack = self.cards_per_pack

        return session_details


async def handle_staked_draft_session(interaction: discord.Interaction, session_details: SessionDetails) -> None:
    """Handle staked draft session creation."""
    session_instance = StakedSession(session_details)
    await session_instance.create_draft_session(interaction, interaction.client)