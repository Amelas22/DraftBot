from .base_session import BaseSession
from discord import Embed, Color

class WinstonSession(BaseSession):
    def create_embed(self):
        title = f"Looking for Players - Winston Draft <t:{self.session_details.draft_start_time}:R>"
        description = (
            "**How to use bot**:\n"
            "1. Click sign up and join the draftmancer link (make sure you set up as a Winston Draft).\n"
            "2. You will be notified after someone joins.\n"
            f"{self.get_common_description()}"
        )
        embed = Embed(title=title, description=description, color=Color.brand_red())
        embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)

        embed.set_thumbnail(url=self.get_thumbnail_url())
  
        return embed

    def get_session_type(self):
        return "winston"
