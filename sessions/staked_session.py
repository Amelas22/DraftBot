from .base_session import BaseSession
from .random_session import RandomSession
from discord import Embed, Color
import logging

logger = logging.getLogger(__name__)

class StakedSession(RandomSession):
    def __init__(self, session_details):
        super().__init__(session_details)
        self.min_stake = 10  # Minimum stake allowed
        
    def create_embed(self):
        """Create an embed message for a staked draft session."""
        title = f"Staked Draft! {self.session_details.cube_choice} - Queue Opened <t:{self.session_details.draft_start_time}:R>"
        description = (
            "**Staked Draft Queue**\n"
            "1. Sign up and enter your max stake amount.\n"
            "2. Teams will be created randomly.\n"
            "3. The bot will match players across teams by stake amount and assign wagers.\n"
            "4. Minimum stake: 10 tix\n\n"
            "**How it works:**\n"
            "• Your max stake is the most you're willing to bet.\n"
            "• When teams are formed, players are matched by stake level.\n"
            "• Each pair's wager will be the minimum of their max stakes.\n"
            "• Leftover stakes may create additional wagers between players.\n"
            f"{self.get_common_description()}"
        )
        embed = Embed(title=title, description=description, color=Color.gold())
        embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)
        
        embed.set_thumbnail(url=self.get_thumbnail_url())
        return embed

    def get_session_type(self):
        return "staked"