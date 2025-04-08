from .base_session import BaseSession
from discord import Embed, Color

class RandomSession(BaseSession):
    def _create_embed_content(self):
        title = f"Looking for Players! {self.session_details.cube_choice} Random Team Draft - Queue Opened <t:{self.session_details.draft_start_time}:R>"
        description = (
            "**How to use bot**:\n"
            "1. Click sign up and click the draftmancer link.\n"
            "2. When enough people join (6 or 8), push Ready Check. Once everyone is ready, push Create Teams.\n"
            "3. Create Teams will create random teams and a corresponding seating order. Draftmancer host needs "
            "to adjust table to match seating order. **TURN OFF RANDOM SEATING IN DRAFTMANCER**\n"
            f"{self.get_common_description()}"
        )
        embed = Embed(title=title, description=description, color=Color.dark_magenta())
        return embed

    def get_session_type(self):
        return "random"
