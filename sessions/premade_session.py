from .base_session import BaseSession
import discord
from sqlalchemy.exc import IntegrityError


class PremadeSession(BaseSession):
    # Remove this method since it overrides the BaseSession.create_embed method
    # Instead, just use the _create_embed_content method with the base class create_embed
    # This method is redundant and causes confusion with initialization

    def get_session_type(self):
        """Return session type for premade sessions."""
        return "premade"

    def get_premade_match_id(self):
        """Provide an actual implementation if premade matches have specific IDs."""
        return super().get_premade_match_id()

    async def create_draft_session(self, interaction, bot):
        """Use base class method to handle the creation of the draft session."""
        match_id = getattr(self.session_details, "tournament_match_id", None)
        if match_id is not None:
            # This is the guard for the common case (one picker open at a time), but
            # it's read-then-act and not atomic: two pickers submitted at once can both
            # pass it. draft_sessions.tournament_match_id's unique index is the actual
            # backstop for that race -- the except below turns the loser's IntegrityError
            # into the same message this guard would have given it.
            from match_control_view import launch_block_for
            block = await launch_block_for(match_id)
            if block is not None:
                await interaction.response.send_message(block, ephemeral=True)
                return
        self.session_details.team_a_name = self.session_details.team_a_name or "Team A"
        self.session_details.team_b_name = self.session_details.team_b_name or "Team B"
        try:
            await super().create_draft_session(interaction, bot)
        except IntegrityError:
            if match_id is None:
                raise
            # Lost the race described above: the other pick's draft committed first and
            # claimed this match, so ours failed the unique index. Nothing has replied
            # to the interaction yet (the base class's send_message is later, past the
            # commit that just raised), so it's safe to answer here -- fail closed, but
            # with the same friendly message the guard above would have given.
            from helpers.match_control import DRAFTING, launch_block_text
            await interaction.response.send_message(
                launch_block_text(DRAFTING, None, ""), ephemeral=True)
            return
        # A launch that already carries a tournament_match_id (▶ Start draft, or
        # /premade_draft inside a match thread) is certain — say so and update the
        # match's control message, instead of guessing with the fuzzy-name nudge.
        if getattr(self.session_details, "tournament_match_id", None) is not None:
            try:
                from match_control_view import announce_and_refresh
                await announce_and_refresh(
                    interaction.client, interaction.channel,
                    self.session_details.tournament_match_id)
            except Exception as e:
                from loguru import logger
                logger.error(f"match control announce failed: {e}")
            return
        # Nudge: if this looks like an ongoing tournament match, offer to link it.
        try:
            from tournament_nudge import post_premade_nudge
            await post_premade_nudge(
                interaction.channel,
                interaction.guild.id,
                self.draft_manager.session_id,
                self.session_details.team_a_name,
                self.session_details.team_b_name,
            )
        except Exception as e:
            from loguru import logger
            logger.error(f"premade tournament nudge failed: {e}")

    def _create_embed_content(self):
        """Create an embed message for a premade draft session."""
        session_details = self.session_details
        # Remove the cube from the title since it's now in its own field
        title = f"Premade Team Draft Queue - Started <t:{session_details.draft_start_time}:R>"
        description = (
            "\n**How to use bot**:\n"
            "1. Click Team A or Team B to join that team. Enter the Draftmancer link. Draftmancer host still has to update settings and import from CubeCobra.\n"
            "2. When all teams are joined, push Ready Check. Once everyone is ready, push Generate Seating Order.\n"
            "3. Draftmancer host needs to adjust the table to match seating order. **TURN OFF RANDOM SEATING IN DRAFTMANCER**.\n"
            "4. After the draft, come back to this message (it'll be in pins) and push Create Rooms and Post Pairings.\n"
            "5. You will now have a private team chat with just your team and a shared draft chat that has pairings and match results. You can select the Match Results buttons to report results.\n"
            "6. Chat channels will automatically close around five hours after the /startdraft command was used."
            f"{self.get_common_description()}"
        )
        embed = discord.Embed(title=title, description=description, color=discord.Color.blue())
        return embed

    def _add_signup_fields(self, embed):
        """Add team-specific fields for premade drafts instead of generic sign-ups."""
        embed.add_field(
            name=self.session_details.team_a_name or "Team A",
            value="No players yet.",
            inline=True,
        )
        embed.add_field(
            name=self.session_details.team_b_name or "Team B",
            value="No players yet.",
            inline=True,
        )
