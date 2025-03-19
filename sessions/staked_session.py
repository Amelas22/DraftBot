from .base_session import BaseSession
from .random_session import RandomSession
from discord import Embed, Color
import logging

logger = logging.getLogger(__name__)

class StakedSession(RandomSession):
    def __init__(self, session_details):
        super().__init__(session_details)
        self.min_stake = session_details.min_stake  
        
    def create_embed(self):
        """Create an embed message for a staked draft session."""
        title = f"{self.session_details.cube_choice} Dynamic Money Draft! Minimum Bet: {self.session_details.min_stake} tix  "
        description = (
            f"Queue Opened <t:{self.session_details.draft_start_time}:R>\n\n"
            "**Dynamic Money Draft Queue**\n"
            "1. Sign up and enter your max bet amount.\n"
            "2. Teams will be created randomly. Max bets are **NOT** factored in when making teams\n"
            f"3. Minimum bet: {self.session_details.min_stake} tix\n\n"
            "**How it works:**\n"
            "• To be written when a methodology is finalized\n"
            "• Basic rules: Ensure both teams can fulfill all 10/20/50 bets on opposing team\n"
            "• If a team cannot, process using Proportional Method\n"
            "• If both teams can, allocate all 10/20/50 bets, then proportionaly distribute all remaining bets\n"
            f"{self.get_common_description()}"
        )
        embed = Embed(title=title, description=description, color=Color.gold())
        embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)
        
        embed.set_thumbnail(url=self.get_thumbnail_url())
        return embed

    def get_session_type(self):
        return "staked"