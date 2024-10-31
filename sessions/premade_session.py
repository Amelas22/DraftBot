from .base_session import BaseSession
import discord


class PremadeSession(BaseSession):
    def create_embed(self):
        """Create an embed message for a premade draft session."""
        session_details = self.session_details
        title = f"{session_details.cube_choice} Premade Team Draft Queue - Started <t:{session_details.draft_start_time}:R>"
        description = (
            "\n**How to use bot**:\n"
            "1. Click Team A or Team B to join that team. Enter the Draftmancer link. Draftmancer host still has to update settings and import from CubeCobra.\n"
            "2. When all teams are joined, push Ready Check. Once everyone is ready, push Generate Seating Order.\n"
            "3. Draftmancer host needs to adjust the table to match seating order. **TURN OFF RANDOM SEATING IN DRAFTMANCER**.\n"
            "4. After the draft, come back to this message (it'll be in pins) and push Create Rooms and Post Pairings.\n"
            "5. You will now have a private team chat with just your team and a shared draft chat that has pairings and match results. You can select the Match Results buttons to report results.\n"
            "6. Chat channels will automatically close around five hours after the /startdraft command was used."
            f"\n\n**Chosen Cube: [{session_details.cube_choice}](https://cubecobra.com/cube/list/{session_details.cube_choice})**\n"
            f"**Draftmancer Session**: **[Join Here]({session_details.draft_link})**"
        )
        color = discord.Color.blue()
        embed = discord.Embed(title=title, description=description, color=color)
        embed.add_field(
            name=session_details.team_a_name or "Team A",
            value="No players yet.",
            inline=True,
        )
        embed.add_field(
            name=session_details.team_b_name or "Team B",
            value="No players yet.",
            inline=True,
        )
        return embed

    def get_session_type(self):
        """Return session type for premade sessions."""
        return "premade"

    def get_premade_match_id(self):
        """Provide an actual implementation if premade matches have specific IDs."""
        return super().get_premade_match_id()

    async def create_draft_session(self, interaction, bot):
        """Use base class method to handle the creation of the draft session."""
        self.session_details.team_a_name = self.session_details.team_a_name or "Team A"
        self.session_details.team_b_name = self.session_details.team_b_name or "Team B"
        await super().create_draft_session(interaction, bot)
