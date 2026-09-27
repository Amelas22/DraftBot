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
            "\n"
            "**How it works:**\n"
            # The cap is ON unless a player turns it off, it runs BEFORE
            # levelling, and until this the queue embed never mentioned it --
            # so the first a trimmed player heard of a default-on setting was
            # the refund. Listed first because that is the order it applies in.
            "• Your entry is **capped to your share of your team** unless you turn "
            "that off, so you are never left funding most of your own side.\n"
            "• Both sides have to be backing the same amount, so when teams form the "
            "heavier side is levelled down and the excess goes straight back to the "
            "players it came from.\n"
            "• Inside a team there is one cut-off and nobody holds more than it: "
            "entries under it are untouched, entries above it are trimmed to it.\n"
            "• Each winner gets back double what they had matched, so the pool pays "
            "out in proportion to what each player held rather than in equal shares.\n"
            f"{self.get_common_description()}"
        )
        embed = Embed(title=title, description=description, color=Color.gold())
        return embed

    def get_session_type(self):
        return "staked"