import discord
import random
from datetime import datetime
from loguru import logger
from typing import Optional
from models.session_details import SessionDetails

from sessions import RandomSession, PremadeSession, SwissSession, BaseSession
from sessions.staked_session import StakedSession

class CubeDraftModal(discord.ui.Modal):
    @classmethod
    def create_if_needed(cls, session_type: str, cube_choice: str) -> Optional['CubeDraftModal']:
        """Create a modal only if it would have input fields."""
        modal = cls(session_type, cube_choice)
        return modal if modal.children else None

    def __init__(self, session_type: str, cube_choice: str, *args, **kwargs) -> None:
        super().__init__(title="Draft Setup", *args, **kwargs)
        self.session_type: str = session_type
        self.cube_choice = cube_choice
        
        if cube_choice == "custom":
            self.add_item(discord.ui.InputText(
                label="Custom Cube Name",
                placeholder="Enter your cube name",
                custom_id="cube_name_input"
            ))
            
        if self.session_type == "premade":
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
        if hasattr(self, 'cube_choice'):
            details.cube_choice = self.cube_choice
        else:
            details.cube_choice = self.children[0].value

        if self.session_type == "premade":
            input_offset = 0 if hasattr(self, 'cube_choice') else 1
            details.team_a_name = self.children[input_offset].value or "Team A"
            details.team_b_name = self.children[input_offset + 1].value or "Team B"

        return details

class CubeDraftSelectionView(discord.ui.View):
    def __init__(self, session_type: str):
        super().__init__()
        self.session_type = session_type
        
        self.cube_select = discord.ui.Select(
            placeholder="Select a Cube",
            options=[
                discord.SelectOption(label="LSVCube", value="LSVCube"),
                discord.SelectOption(label="AlphaFrog", value="AlphaFrog"),
                discord.SelectOption(label="modovintage", value="modovintage"),
                discord.SelectOption(label="LSVRetro", value="LSVRetro"),
                discord.SelectOption(label="PowerMack", value="PowerMack"),
                discord.SelectOption(label="Custom Cube...", value="custom")
            ]
        )
        self.cube_select.callback = self.cube_select_callback
        self.add_item(self.cube_select)

    async def cube_select_callback(self, interaction: discord.Interaction):
        cube_choice = self.cube_select.values[0]
        
        if modal := CubeDraftModal.create_if_needed(self.session_type, cube_choice):
            await interaction.response.send_modal(modal)
        else:
            session_details = SessionDetails(interaction)
            session_details.cube_choice = cube_choice
            await handle_draft_session(interaction, self.session_type, session_details)

# Shared utility functions
def create_session_instance(session_type: str, session_details: SessionDetails) -> BaseSession:
    """Dynamically create the appropriate session instance based on the session type."""
    session_class: type = {
        "premade": PremadeSession,
        "swiss": SwissSession,
        'random': RandomSession,
    }.get(session_type, BaseSession)

    return session_class(session_details)

async def handle_draft_session(interaction: discord.Interaction, session_type: str, session_details: SessionDetails) -> None:
    """Handle either scheduled or immediate draft sessions."""
    if session_type == "schedule":
        from league import InitialPostView
        initial_view = InitialPostView(
            command_type=session_type,
            team_id=1,
            cube_choice=session_details.cube_choice
        )
        await interaction.response.send_message(
            "Post a scheduled draft. Select a Timezone.",
            view=initial_view,
            ephemeral=True
        )
        return

    session_instance = create_session_instance(session_type, session_details)
    await session_instance.create_draft_session(interaction, interaction.client)


class StakedCubeDraftSelectionView(discord.ui.View):
    def __init__(self):
        super().__init__()
        
        self.cube_select = discord.ui.Select(
            placeholder="Select a Cube",
            options=[
                discord.SelectOption(label="LSVCube", value="LSVCube"),
                discord.SelectOption(label="AlphaFrog", value="AlphaFrog"),
                discord.SelectOption(label="modovintage", value="modovintage"),
                discord.SelectOption(label="LSVRetro", value="LSVRetro"),
                discord.SelectOption(label="PowerMack", value="PowerMack"),
                discord.SelectOption(label="Custom Cube...", value="custom")
            ]
        )
        self.cube_select.callback = self.cube_select_callback
        self.add_item(self.cube_select)

    async def cube_select_callback(self, interaction: discord.Interaction):
        cube_choice = self.cube_select.values[0]
        
        # Always show the staked draft modal
        modal = StakedCubeDraftModal(cube_choice)
        await interaction.response.send_modal(modal)


class StakedCubeDraftModal(discord.ui.Modal):
    def __init__(self, cube_choice: str, *args, **kwargs) -> None:
        super().__init__(title="Staked Draft Setup", *args, **kwargs)
        self.cube_choice = cube_choice
        
        if cube_choice == "custom":
            self.add_item(discord.ui.InputText(
                label="Custom Cube Name",
                placeholder="Enter your cube name",
                custom_id="cube_name_input"
            ))

        # Add min stake input
        self.add_item(discord.ui.InputText(
            label="Minimum Stake (tix)",
            placeholder="Enter minimum stake (default: 10)",
            custom_id="min_stake_input",
            required=False
        ))
            
    async def callback(self, interaction: discord.Interaction) -> None:
        # Configure session details
        session_details = SessionDetails(interaction)
        
        # If custom cube, get name from input, otherwise use preset choice
        if self.cube_choice == "custom":
            session_details.cube_choice = self.children[0].value
            input_offset = 1
        else:
            session_details.cube_choice = self.cube_choice
            input_offset = 0
        
        # Get min stake if provided
        min_stake_str = self.children[input_offset].value
        min_stake = 10  
        if min_stake_str:
            try:
                min_stake = int(min_stake_str)
                if min_stake < 1:
                    min_stake = 10 
            except ValueError:
                pass  
        
        # Store min stake in session details
        session_details.min_stake = min_stake
        
        # Create and start the draft session
        session_instance = StakedSession(session_details)
        await session_instance.create_draft_session(interaction, interaction.client)