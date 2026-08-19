"""Discord glue for a tournament match's control message.

Top-level module (like tournament_nudge.py) so the tournament cog, the draft
cog, the premade session hook, utils.py and views.py can all import it without
an import cycle through cogs/. The message body itself is rendered by
helpers/match_control.py, which stays free of Discord imports.
"""
from typing import Any, Callable, Coroutine, cast

import discord
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.db_session import db_session
from helpers.match_control import (
    SCHEDULING,
    launch_block_text,
    match_state,
    recorded_result_line,
    render_match_control,
)
from helpers.pin_helpers import safe_pin
from helpers.utils import not_none
from models.draft_session import DraftSession
from models.tournament import TournamentMatch, TournamentParticipant, TournamentRound


async def match_facts(
    session: AsyncSession, match_id: int
) -> tuple[TournamentMatch, str, str, int, DraftSession | None] | None:
    """(match, a_name, b_name, round_number, draft) for a match, or None.

    ``draft`` is the DraftSession linked to this match, or None. One query set
    feeding every render, so the state can never be derived from a half-stale
    picture.
    """
    row = (await session.execute(
        select(TournamentMatch, TournamentRound.round_number)
        .join(TournamentRound, TournamentMatch.round_id == TournamentRound.id)
        .where(TournamentMatch.id == match_id)
    )).first()
    if row is None:
        return None
    match, round_number = row
    part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
    part_b = await session.get(TournamentParticipant, match.team_b_participant_id)
    if part_a is None or part_b is None:
        return None
    draft = (await session.execute(
        select(DraftSession).where(DraftSession.tournament_match_id == match_id)
    )).scalars().first()
    return match, part_a.team_name, part_b.team_name, round_number, draft


def lobby_link(draft: DraftSession | None) -> str | None:
    """Jump URL for a linked draft's lobby message, or None."""
    if draft is None or not draft.message_id or not draft.draft_channel_id:
        return None
    return (f"https://discord.com/channels/{draft.guild_id}/"
            f"{draft.draft_channel_id}/{draft.message_id}")


def control_body_and_view(
    match: TournamentMatch, a_name: str, b_name: str, round_number: int, draft: DraftSession | None
) -> tuple[str, "MatchControlView | None"]:
    """(body, view) for a match's control message. View is None off `scheduling`."""
    state = match_state(match.team_a_wins is not None, draft is not None)
    body = render_match_control(
        state, a_name, b_name, round_number,
        lobby_link=lobby_link(draft),
        result=(match.team_a_wins, match.team_b_wins),
    )
    return body, (MatchControlView(match.id) if state == SCHEDULING else None)


def cube_picker_for_match(guild_id: int | None, match_id: int, a_name: str, b_name: str) -> discord.ui.View:
    """The premade cube picker, pre-named from the pairing.

    Both team names present is what makes CubeDraftModal drop its name inputs
    (modals.py), so the launcher cannot be given the wrong names.
    """
    from modals import CubeDraftSelectionView
    return CubeDraftSelectionView(
        session_type="premade",
        guild_id=guild_id,
        session_details_overrides={
            "tournament_match_id": match_id,
            "team_a_name": a_name,
            "team_b_name": b_name,
        },
    )


class MatchControlView(discord.ui.View):
    """Persistent 'Start draft' button on a match's control message."""

    def __init__(self, match_id: int) -> None:
        super().__init__(timeout=None)
        button: discord.ui.Button[Any] = discord.ui.Button(
            label="Start draft",
            style=discord.ButtonStyle.primary,
            custom_id=f"tournament_start_draft:{match_id}",
        )
        # discord.ui.Item.callback is declared on the class as an unbound method
        # (self, interaction), so pyrefly sees the attribute type as still
        # expecting `self` even though py-cord calls it bound at dispatch time.
        button.callback = self._make_callback(match_id)  # pyrefly: ignore [bad-assignment]
        self.add_item(button)

    @staticmethod
    def _make_callback(match_id: int) -> Callable[[discord.Interaction], Coroutine[Any, Any, None]]:
        async def callback(interaction: discord.Interaction) -> None:
            await start_match_draft(interaction, match_id)
        return callback


async def _resolve_thread(
    interaction: discord.Interaction, thread_id: "str | None", a_name: str, b_name: str
) -> discord.Thread:
    """The match's thread, adopting or creating one as needed."""
    if thread_id:
        guild = not_none(interaction.guild)
        channel = guild.get_channel(int(thread_id))
        if channel is None:
            channel = await guild.fetch_channel(int(thread_id))
        # The stored id always names a thread this feature created or adopted;
        # get_channel/fetch_channel's wider return type covers arbitrary channel ids.
        return cast(discord.Thread, channel)
    # A thread may already hang off the pairing message (an organiser made it by
    # hand). Discord rejects a second thread on the same message, so adopt it.
    message = not_none(interaction.message)
    if message.thread is not None:
        return message.thread
    return await message.create_thread(name=f"{a_name} vs {b_name}"[:100])


async def _resolve_control_message(
    thread: discord.Thread, control_id: "str | None", body: str, view: "MatchControlView | None"
) -> discord.Message:
    """The pinned control message, editing an existing one or posting a new one."""
    if control_id:
        try:
            message = await thread.fetch_message(int(control_id))
            await message.edit(content=body, view=view)
            return message
        except discord.NotFound:
            logger.warning(
                f"Control message {control_id} missing in thread {thread.id}; reposting")
    # Messageable.send's view overloads don't accept an explicit None (only a
    # real View or the omitted-argument default), even though py-cord treats
    # a falsy view as "no view" at runtime.
    if view is not None:
        message = await thread.send(content=body, view=view)
    else:
        message = await thread.send(content=body)
    await safe_pin(message)
    return message


async def open_match_room(interaction: discord.Interaction, match_id: int) -> None:
    """▶ Play: open the match's room. Never starts a draft.

    Resolve-or-create the per-match thread and its pinned control message, then
    point the clicker at the thread. Idempotent: any number of clicks converge
    on one thread and one control message.
    """
    async with db_session() as session:
        facts = await match_facts(session, match_id)
        if facts is None:
            await interaction.followup.send("This match no longer exists.", ephemeral=True)
            return
        match, a_name, b_name, round_number, draft = facts
        thread_id, control_id = match.thread_id, match.control_message_id
        body, view = control_body_and_view(match, a_name, b_name, round_number, draft)

    thread = await _resolve_thread(interaction, thread_id, a_name, b_name)
    message = await _resolve_control_message(thread, control_id, body, view)

    async with db_session() as session:
        stored = await session.get(TournamentMatch, match_id)
        if stored is not None:
            stored.thread_id = str(thread.id)
            stored.control_message_id = str(message.id)

    await interaction.followup.send(f"Your match room is {thread.mention}.", ephemeral=True)


async def start_match_draft(interaction: discord.Interaction, match_id: int) -> None:
    """'Start draft' button: post the cube picker into the match thread."""
    async with db_session() as session:
        facts = await match_facts(session, match_id)
        if facts is None:
            await interaction.response.send_message(
                "This match no longer exists.", ephemeral=True)
            return
        match, a_name, b_name, round_number, draft = facts
        body, view = control_body_and_view(match, a_name, b_name, round_number, draft)
        block = launch_block_text(
            match_state(match.team_a_wins is not None, draft is not None),
            lobby_link(draft),
            recorded_result_line(a_name, b_name, match.team_a_wins, match.team_b_wins),
        )

    if block is not None:
        # Re-render rather than only complaining: whatever changed underneath is
        # now visible to everyone, not just the clicker.
        await interaction.response.edit_message(content=body, view=view)
        await interaction.followup.send(block, ephemeral=True)
        return

    await interaction.response.send_message(
        content=(f"Pick a cube to start **{a_name}** vs **{b_name}** "
                 f"(the result records automatically when the draft finishes):"),
        view=cube_picker_for_match(interaction.guild_id, match_id, a_name, b_name),
    )


async def refresh_match_control(bot: discord.Client, match_id: int) -> None:
    """Re-render a match's control message in place. No-op if never posted."""
    async with db_session() as session:
        facts = await match_facts(session, match_id)
        if facts is None:
            return
        match, a_name, b_name, round_number, draft = facts
        if not match.control_message_id or not match.thread_id:
            return
        thread_id, control_id = int(match.thread_id), int(match.control_message_id)
        body, view = control_body_and_view(match, a_name, b_name, round_number, draft)

    channel = bot.get_channel(thread_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(thread_id)
        except discord.HTTPException:
            logger.warning(f"Match {match_id} thread {thread_id} unreachable; not refreshed")
            return
    # The stored id always names a thread opened by open_match_room; the
    # wider return type here covers channel ids in general.
    thread = cast(discord.Thread, channel)
    try:
        message = await thread.fetch_message(control_id)
        await message.edit(content=body, view=view)
    except discord.NotFound:
        logger.warning(f"Match {match_id} control message {control_id} gone; not refreshed")
    except discord.HTTPException as e:
        logger.error(f"Failed to refresh control message for match {match_id}: {e}")


async def announce_and_refresh(
    bot: discord.Client, channel: discord.abc.Messageable, match_id: int
) -> None:
    """Say the draft counts, then flip the match's control message to 'drafting'.

    One path for both entry points (Start draft and /premade_draft in the
    thread), so a draft that counts always says so the same way.
    """
    async with db_session() as session:
        facts = await match_facts(session, match_id)
    if facts is not None:
        _match, a_name, b_name, round_number, _draft = facts
        await channel.send(
            f"🔗 Linked to Round {round_number} — **{a_name}** vs **{b_name}**. "
            "The result will record automatically.")
    await refresh_match_control(bot, match_id)
