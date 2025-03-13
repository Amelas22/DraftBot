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
        title = f"Dynamic Money Draft! {self.session_details.cube_choice}"
        description = (
            f"Queue Opened <t:{self.session_details.draft_start_time}:R>\n\n"
            "**Dynamic Money Draft Queue**\n"
            "1. Sign up and enter your max bet amount.\n"
            "2. Teams will be created randomly. Max bets are **NOT** factored in when making teams\n"
            "3. Minimum stake: 10 tix\n\n"
            "**How it works:**\n"
            "• Your max bet is the most you're willing to wager across one (or more) bets.\n"
            "• Teams will be randomized **without** factoring in the max bets.\n"
            "• After teams are formed, players are ranked from highest bet to lowest bet.\n"
            "• Players are then paired with the opposing player of the same rank.\n"
            "• Each pair's wager will be the **minimum** of their max bet.\n"
            "• If players have unfilled bets, a potential second bet could be formed.\n\n\n"
            "**Click the 'How Stakes Work' button below for a more detailed explanation.**\n"
            f"{self.get_common_description()}"
        )
        embed = Embed(title=title, description=description, color=Color.gold())
        embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)
        
        embed.set_thumbnail(url=self.get_thumbnail_url())
        return embed

    def get_session_type(self):
        return "staked"