from .base_session import BaseSession
from .random_session import RandomSession
from discord import Embed, Color
import logging

logger = logging.getLogger(__name__)

class WinstonSession(RandomSession):
    def __init__(self, session_details):
        super().__init__(session_details)
        self.min_stake = getattr(session_details, 'min_stake', 10)
        self.max_stake = getattr(session_details, 'max_stake', self.min_stake)
        # Store creator info from session_details
        self.creator_id = getattr(session_details, 'creator_id', None)
        self.creator_name = getattr(session_details, 'creator_name', 'Unknown')
        
    def create_embed(self):
        """Create an embed message for a dynamic winston draft session."""
        title = f"{self.session_details.cube_choice} Dynamic Winston Draft! Minimum Bet: {self.session_details.min_stake} tix"
        description = (
            f"Queue Opened <t:{self.session_details.draft_start_time}:R>\n\n"
            "**Dynamic Winston Draft Queue**\n"
            "• First player sets the minimum bet for the queue\n"
            "• Second player's max bet must meet or exceed this minimum\n"
            "• Actual bet will be the minimum of both players' max bets\n\n"
            f"Minimum bet: {self.session_details.min_stake} tix\n\n"
            f"{self.get_common_description()}"
        )
        embed = Embed(title=title, description=description, color=Color.gold())
        
        # Add creator as the first sign-up with max bet
        if self.creator_id:
            sign_ups = {self.creator_id: self.creator_name}
            self.session_details.sign_ups = sign_ups
            
            embed.add_field(
                name=f"Sign-Ups (1/2)", 
                value=f"{self.creator_name} - Max Bet: {self.max_stake} tix", 
                inline=False
            )
        else:
            embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)
        
        embed.set_thumbnail(url=self.get_thumbnail_url())
        return embed

    def get_session_type(self):
        return "dynamic_winston"
        
    def setup_draft_session(self, session):
        """Override setup_draft_session to add the creator to sign_ups and create StakeInfo"""
        draft_session = super().setup_draft_session(session)
        
        # Add creator to sign_ups if we have creator info
        if self.creator_id:
            draft_session.sign_ups = {self.creator_id: self.creator_name}
            
            # Create StakeInfo for creator
            from session import StakeInfo
            stake_info = StakeInfo(
                session_id=draft_session.session_id,
                player_id=self.creator_id,
                max_stake=self.max_stake,
                assigned_stake=0,  # Will be calculated when second player joins
                is_capped=True
            )
            session.add(stake_info)
        
        return draft_session