from .base_session import BaseSession
from .random_session import RandomSession
from discord import Embed, Color
import logging

logger = logging.getLogger(__name__)

class StakedSession(RandomSession):
    def __init__(self, session_details, session_factory=None):
        super().__init__(session_details, session_factory=session_factory)
        self.min_stake = session_details.min_stake  
        
    def _create_embed_content(self):
        """Create an embed message for a staked draft session."""
        # Remove the cube from the title since it's now in its own field
        title = (f"Prize Pool Draft! Minimum entry: "
                 f"{self.session_details.min_stake} tix")
        description = (
            f"Queue Opened <t:{self.session_details.draft_start_time}:R>\n\n"
            "**Prize Pool Draft Queue**\n"
            "1. Sign up and set your maximum entry. It leaves your wallet now "
            "and goes into the prize pool.\n"
            "2. Teams are drawn at random. Entries play **no** part in who ends "
            "up on which team.\n"
            f"3. Minimum entry: {self.session_details.min_stake} tix\n\n"
            "**How it works:**\n"
            "• Both sides have to be backing the same amount, so when teams "
            "form the heavier side is levelled down and the difference comes "
            "straight back to your wallet.\n"
            "• Within a team, every entry fills up to a common ceiling: smaller "
            "entries are kept whole and the largest carry the shortfall.\n"
            "• The winning team splits the pool: whatever you had matched, you "
            "get back double.\n"
            f"{self.get_common_description()}"
        )
        embed = Embed(title=title, description=description, color=Color.gold())
        return embed

    def get_session_type(self):
        return "staked"