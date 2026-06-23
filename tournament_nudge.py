"""Discord views + glue for the premade -> tournament-match link nudge.

Top-level module (like livedrafts.py) so the premade session hook and bot.py
can both import it without a cog import cycle.
"""
import discord
from loguru import logger
from sqlalchemy import select

from database.db_session import db_session
from models.draft_session import DraftSession
from services.tournament_linking import link_draft_to_match, match_summary, resolve_candidate_matches
from services.tournament_service import get_active_tournament


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


async def perform_link(session_id, match_id, actor_id):
    """Open a session, link, log the actor on success. db_session() commits on exit."""
    async with db_session() as session:
        outcome = await link_draft_to_match(session, session_id, match_id, actor_id)
    # No extra commit: db_session() commits on normal exit; failure paths made no writes.
    if outcome.status == "linked":
        logger.info(f"Premade draft {session_id} linked to tournament match "
                    f"{match_id} by {actor_id}")
    return outcome


_FAIL_TEXT = {
    "already_linked": "This draft is already linked to a tournament match.",
    "match_played": "That match already has a recorded result.",
    "match_taken": "That match was just linked to another draft.",
    "no_match": "That match no longer exists.",
}


async def apply_confirmation(session_id, match_id, actor_id, actor_mention, public_message):
    """Link, edit the public nudge message (dropping its control), return ephemeral note.

    On success: attribution note naming the match + the acting user. On any
    terminal failure: a failure note. Either way the public control is removed.
    """
    outcome = await perform_link(session_id, match_id, actor_id)
    if outcome.status == "linked":
        public_note = (f"✅ Linked to **{outcome.a_name}** vs **{outcome.b_name}** "
                       f"(Round {outcome.round_number}) by {actor_mention} — "
                       f"the result records automatically.")
        ephemeral = "Linked. ✅"
    else:
        public_note = f"❌ {_FAIL_TEXT.get(outcome.status, 'Could not link.')}"
        ephemeral = public_note
    if public_message is not None:
        try:
            await public_message.edit(content=public_note, view=None)
        except discord.HTTPException:
            pass
    return ephemeral


class LinkConfirmView(discord.ui.View):
    """Ephemeral Confirm/Cancel shown after a button click or dropdown pick."""

    def __init__(self, session_id, match_id, public_message):
        super().__init__(timeout=120)
        self.session_id = session_id
        self.match_id = match_id
        self.public_message = public_message  # the public nudge message to edit

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, button, interaction):
        note = await apply_confirmation(
            self.session_id, self.match_id, str(interaction.user.id),
            interaction.user.mention, self.public_message)
        await interaction.response.edit_message(content=note, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button, interaction):
        await interaction.response.edit_message(content="Cancelled.", view=None)


async def prompt_link_confirmation(interaction, session_id, match_id):
    async with db_session() as session:
        summary = await match_summary(session, match_id)
    if summary is None:
        await interaction.response.send_message("That match no longer exists.", ephemeral=True)
        return
    a_name, b_name, round_number = summary
    view = LinkConfirmView(session_id, match_id, interaction.message)
    await interaction.response.send_message(
        content=(f"Link this draft to **{a_name}** vs **{b_name}** (Round {round_number})? "
                 f"The result will record into the tournament automatically when the "
                 f"draft finishes."),
        view=view, ephemeral=True,
    )


async def post_premade_nudge(channel, guild_id, session_id, team_a_name, team_b_name):
    """Resolve candidates for a freshly-created premade draft and post the nudge.

    No-op when the guild has no active tournament, the draft is already linked
    (e.g. a Play-button launch sets tournament_match_id), or no candidate fits.
    """
    async with db_session() as session:
        draft = (await session.execute(
            select(DraftSession).where(DraftSession.session_id == session_id)
        )).scalars().first()
        # If the draft exists and is already linked (e.g. Play-button launch), skip nudge.
        if draft is not None and draft.tournament_match_id is not None:
            return
        tournament = await get_active_tournament(session, guild_id)
        if tournament is None:
            return
        candidates = await resolve_candidate_matches(
            session, tournament, team_a_name, team_b_name)
    built = build_nudge_view(session_id, candidates)
    if built is None:
        return
    content, view = built
    try:
        await channel.send(content=content, view=view)
    except discord.HTTPException as e:
        logger.warning(f"Could not post tournament-link nudge for {session_id}: {e}")
