import discord
import random
from datetime import datetime
from loguru import logger
from typing import Optional
from models.session_details import SessionDetails

from sessions import RandomSession, PremadeSession, SwissSession, BaseSession

class CubeSelectionModal(discord.ui.Modal):
    def __init__(self, session_type: str, *args, **kwargs) -> None:
        super().__init__(title="Cube Selection", *args, **kwargs)
        self.session_type: str = session_type
        self.add_item(discord.ui.InputText(label="Cube Name", placeholder="LSVCube, AlphaFrog, MOCS24, or your choice", custom_id="cube_name_input"))
        if self.session_type == "premade":
            self.add_item(discord.ui.InputText(label="Team A Name", placeholder="Enter Team A Name", custom_id="team_a_input"))
            self.add_item(discord.ui.InputText(label="Team B Name", placeholder="Enter Team B Name", custom_id="team_b_input"))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        # Create and configure session details from user input
        session_details: SessionDetails = self.configure_session_details(interaction)
        # Handle the scheduled session type with a specific message
        if self.session_type == "schedule":
            from league import InitialPostView
            initial_view = InitialPostView(
                command_type=self.session_type, 
                team_id=1, 
                cube_choice=session_details.cube_choice
            )
            await interaction.response.send_message(
                "Post a scheduled draft. Select a Timezone.", 
                view=initial_view, 
                ephemeral=True
            )
            return
        # Delegate to the correct session type
        session_instance: BaseSession = self.create_session_instance(session_details)
        await session_instance.create_draft_session(interaction, bot)

    def configure_session_details(self, interaction: discord.Interaction) -> SessionDetails:
        """Prepare the session details based on the user input."""
        details: SessionDetails = SessionDetails(interaction)
        details.cube_choice = self.children[0].value

        if self.session_type == "premade":
            details.team_a_name = self.children[1].value or "Team A"
            details.team_b_name = self.children[2].value or "Team B"

        return details

    def create_session_instance(self, session_details: SessionDetails) -> BaseSession:
        """Dynamically create the appropriate session instance based on the session type."""
        session_class: type = {
            "premade": PremadeSession,
            "swiss": SwissSession,
            'random': RandomSession,
        }.get(self.session_type, BaseSession)  # Default to BaseSession if type is not recognized

        return session_class(session_details)
