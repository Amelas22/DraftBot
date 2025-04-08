from .base_session import BaseSession
import discord

class SwissSession(BaseSession):
    def _create_embed_content(self):
        """Create an embed message for a Swiss draft session."""
        session_details = self.session_details
        title = f"AlphaFrog Prelims: Looking for Players! Queue Opened <t:{session_details.draft_start_time}:R>"
        description = (
            "Swiss 8 player draft. Draftmancer host must still update the draftmancer session with the chosen cube."
            f"{self.get_common_description()}"
        )
        color = discord.Color.dark_gold()
        embed = discord.Embed(title=title, description=description, color=color)
        return embed

    def get_session_type(self):
        """Return session type for Swiss sessions."""
        return "swiss"

    def get_premade_match_id(self):
        """Provide a specific premade match ID for Swiss sessions."""
        return 9000
    
    def get_session_buttons(self, view):
        """Add Swiss-specific buttons."""
        view.add_item(view.create_button("Generate Seating Order", "blurple", f"randomize_teams_{view.draft_session_id}", view.randomize_teams_callback))
    
    async def create_draft_session(self, interaction, bot):
        """Use base class method to handle the creation of the draft session."""
        await super().create_draft_session(interaction, bot)
