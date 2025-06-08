from .base_session import BaseSession
from .random_session import RandomSession
from discord import Embed, Color
import logging

logger = logging.getLogger(__name__)

class StakedSession(RandomSession):
    def __init__(self, session_details):
        super().__init__(session_details)
        self.min_stake = session_details.min_stake  
        
    def _create_embed_content(self):
        """Create an embed message for a staked draft session."""
        # Remove the cube from the title since it's now in its own field
        title = f"Dynamic Money Draft! Minimum Bet: {self.session_details.min_stake} tix"
        description = (
            f"Queue Opened <t:{self.session_details.draft_start_time}:R>\n\n"
            "**Dynamic Money Draft Queue**\n"
            "1. Sign up and enter your max bet amount.\n"
            "2. Players can choose to cap their bets to the opponents max bet (🧢) or to take as much action as possible (🏎️) up to their max bet.\n"
            "3. Teams will be created randomly. Max bets are **NOT** factored in when making teams\n"
            f"4. Minimum bet: {self.session_details.min_stake} tix\n\n"
            "**How it works:**\n"
            "• Basic rules: Ensure both teams can fulfill all 20/50 bets on opposing team\n"
            "• If a team cannot, process using a Proportional Method\n"
            "• If both teams can, allocate all 20/50 bets, then proportionaly distribute all remaining bets\n"
            f"{self.get_common_description()}"
        )
        embed = Embed(title=title, description=description, color=Color.gold())
        return embed

    def get_session_type(self):
        return "staked"