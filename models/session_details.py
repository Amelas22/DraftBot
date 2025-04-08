from datetime import datetime
import random
import discord
from loguru import logger
from config import get_draftmancer_session_url

class SessionDetails:
    def __init__(self, interaction: discord.Interaction, draft_start_time=None):
        self.draft_start_time = draft_start_time or int(datetime.now().timestamp())
        self.session_id = f"{interaction.user.id}-{self.draft_start_time}"
        self.draft_id = ''.join(random.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
        self.draft_link = get_draftmancer_session_url(self.draft_id)
        self.guild_id = str(interaction.guild_id)
        self.cube_choice = None
        self.team_a_name = None
        self.team_b_name = None
        self.min_stake = 10
        logger.debug(f"SessionDetails initialized: {self}")
