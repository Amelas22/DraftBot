"""Discord views + glue for the premade -> tournament-match link nudge.

Top-level module (like livedrafts.py) so the premade session hook and bot.py
can both import it without a cog import cycle.
"""
import discord
from loguru import logger

from database.db_session import db_session
from services.tournament_linking import link_draft_to_match


def _match_label(c):
    return f"{c.a_name} vs {c.b_name} — Round {c.round_number}"


class TournamentLinkButtonView(discord.ui.View):
    """Persistent one-button nudge for the single-candidate case."""

    def __init__(self, session_id, match_id, label):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label=f"▶ Link to {label}"[:80],
            style=discord.ButtonStyle.primary,
            custom_id=f"tourney_link:{session_id}:{match_id}",
        )
        button.callback = self._callback
        self.add_item(button)

    async def _callback(self, interaction):
        # custom_id = tourney_link:<session_id>:<match_id>
        _, session_id, match_id = interaction.data["custom_id"].split(":", 2)
        await prompt_link_confirmation(interaction, session_id, int(match_id))


class TournamentLinkSelectView(discord.ui.View):
    """Persistent dropdown nudge for the multiple-candidate case."""

    def __init__(self, session_id, candidates):
        super().__init__(timeout=None)
        options = [
            discord.SelectOption(label=_match_label(c)[:100], value=str(c.match_id))
            for c in candidates
        ]
        select = discord.ui.Select(
            placeholder="Pick the tournament match this draft is…",
            min_values=1, max_values=1, options=options,
            custom_id=f"tourney_link_select:{session_id}",
        )
        select.callback = self._callback
        self.add_item(select)

    async def _callback(self, interaction):
        _, session_id = interaction.data["custom_id"].split(":", 1)
        match_id = int(interaction.data["values"][0])
        await prompt_link_confirmation(interaction, session_id, match_id)


def build_nudge_view(session_id, candidates):
    """Return (content, view) for the nudge, or None when there are no candidates."""
    if not candidates:
        return None
    if len(candidates) == 1:
        c = candidates[0]
        content = (f"⚠️ This looks like a tournament match: **{c.a_name}** vs "
                   f"**{c.b_name}** (Round {c.round_number}). Link it so the result "
                   f"records into the tournament automatically?")
        return content, TournamentLinkButtonView(session_id, c.match_id, _match_label(c))
    content = ("⚠️ This looks like a tournament match, but more than one fits. "
               "Pick the match it corresponds to:")
    return content, TournamentLinkSelectView(session_id, candidates)


async def prompt_link_confirmation(interaction, session_id, match_id):
    raise NotImplementedError  # implemented in Task 4
