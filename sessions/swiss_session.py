from .base_session import BaseSession
import discord

class SwissSession(BaseSession):
    def create_embed(self):
        """Create an embed message for a Swiss draft session."""
        session_details = self.session_details
        title = f"AlphaFrog Prelims: Looking for Players! Queue Opened <t:{session_details.draft_start_time}:R>"
        description = (
            "Swiss 8 player draft. Draftmancer host must still update the draftmancer session with the chosen cube."
            f"{self.get_common_description()}"
        )
        color = discord.Color.dark_gold()
        embed = discord.Embed(title=title, description=description, color=color)
        embed.add_field(name="Sign-Ups", value="No players yet.", inline=False)
        embed.set_thumbnail(url=self.get_thumbnail_url())
        return embed

    def get_session_type(self):
        """Return session type for Swiss sessions."""
        return "swiss"

    def get_premade_match_id(self):
        """Provide a specific premade match ID for Swiss sessions."""
        return 9000
    
    async def create_draft_session(self, interaction, bot):
        """Use base class method to handle the creation of the draft session."""
        await super().create_draft_session(interaction, bot)
