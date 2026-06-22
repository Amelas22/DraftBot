from datetime import datetime
import random
import discord
from loguru import logger
from config import get_draftmancer_session_url
from helpers.utils import not_none

class SessionDetails:
    def __init__(self, interaction: discord.Interaction, draft_start_time=None):
        self.draft_start_time = draft_start_time or int(datetime.now().timestamp())
        self.session_id = f"{not_none(interaction.user).id}-{self.draft_start_time}"
        self.draft_id = ''.join(random.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
        self.draft_link = get_draftmancer_session_url(self.draft_id)
        self.guild_id = str(interaction.guild_id) if interaction.guild_id is not None else None
        self.cube_choice = None
        self.team_a_name = None
        self.team_b_name = None
        self.min_stake = 10
        self.tournament_match_id = None  # set when launched from a tournament pairing
        self.packs_per_player = 3
        self.cards_per_pack = 15
        logger.debug(f"SessionDetails initialized: {self}")
