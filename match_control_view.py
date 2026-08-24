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
    render_pairing_line,
)
from helpers.pin_helpers import safe_pin
from helpers.utils import DISCORD_THREAD_NAME_LIMIT, THREAD_ARCHIVE_MAX_MINUTES, as_messageable
from models.draft_session import DraftSession
from models.tournament import (
    Tournament,
    TournamentMatch,
    TournamentParticipant,
    TournamentRound,
)
from services.tournament_formatter import round_label


async def match_facts(
    session: AsyncSession, match_id: int
) -> tuple[TournamentMatch, str, str, str, DraftSession | None] | None:
    """(match, a_name, b_name, round_label, draft) for a match, or None.

    ``draft`` is the DraftSession linked to this match, or None. One query set
    feeding every render, so the state can never be derived from a half-stale
    picture.

    ``round_label`` is the round's name rather than its number: a bracket round
    is numbered past the swiss total, so "Round 4" named the semifinal of a
    3-round swiss after a swiss round nobody played.
    """
    row = (await session.execute(
        select(TournamentMatch, TournamentRound.round_number, TournamentRound.stage,
               Tournament.total_rounds)
        .join(TournamentRound, TournamentMatch.round_id == TournamentRound.id)
        .join(Tournament, TournamentRound.tournament_id == Tournament.id)
        .where(TournamentMatch.id == match_id)
    )).first()
    if row is None:
        return None
    match, round_number, stage, total_rounds = row
    label = round_label(total_rounds, round_number, stage)
    part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
    part_b = await session.get(TournamentParticipant, match.team_b_participant_id)
    if part_a is None or part_b is None:
        return None
    draft = (await session.execute(
        select(DraftSession).where(DraftSession.tournament_match_id == match_id)
    )).scalars().first()
    return match, part_a.team_name, part_b.team_name, label, draft


def lobby_link(draft: DraftSession | None) -> str | None:
    """Jump URL for a linked draft's lobby message, or None."""
    if draft is None or not draft.message_id or not draft.draft_channel_id:
        return None
    return (f"https://discord.com/channels/{draft.guild_id}/"
            f"{draft.draft_channel_id}/{draft.message_id}")


def block_for_facts(
    facts: tuple[TournamentMatch, str, str, str, DraftSession | None],
) -> str | None:
    """launch_block_text for an already-fetched facts tuple, or None.

    match_state -> lobby_link -> recorded_result_line -> launch_block_text is
    the derivation every caller that already holds a match's facts needs
    (the Start draft button, /premade_draft's match-thread context, and the
    draft-creation guard in sessions/premade_session.py); written once here
    so the three cannot render "why not" differently for the same state.
    """
    match, a_name, b_name, _label, draft = facts
    return launch_block_text(
        match_state(match.team_a_wins is not None, draft is not None),
        lobby_link(draft),
        recorded_result_line(a_name, b_name, match.team_a_wins, match.team_b_wins),
    )


def control_body_and_view(
    match: TournamentMatch, a_name: str, b_name: str, label: str, draft: DraftSession | None,
    role_mentions: tuple[str | None, str | None] | None = None,
) -> tuple[str, "MatchControlView | None"]:
    """(body, view) for a match's control message. View is None off `scheduling`.

    role_mentions is only ever supplied by create_match_room, at the message's
    first post. start_match_draft and _refresh_match_views_with_facts re-render
    this same message on later refreshes and deliberately leave it out:

    - The mention's job is done the instant the message is first posted: it
      notifies both teams and adds them to the thread, and that thread
      membership persists whatever the message later says.
    - A mention is a live query against guild state, not a record (the same
      property that keeps pairings posts as plain names). Once the tournament
      ends and its roles are deleted, a mention preserved in the body would
      re-render as @deleted-role -- leaving it out of refreshes means the
      finished match room reads cleanly.
    """
    state = match_state(match.team_a_wins is not None, draft is not None)
    body = render_match_control(
        state, a_name, b_name, label,
        lobby_link=lobby_link(draft),
        result=(match.team_a_wins, match.team_b_wins),
        role_mentions=role_mentions,
    )
    return body, (MatchControlView(match.id) if state == SCHEDULING else None)


def _picker_overrides(match_id: int, a_name: str, b_name: str) -> dict[str, Any]:
    """The session_details_overrides a draft launched against a match must carry.

    The three keys travel together on purpose: CubeDraftModal only drops its
    team-name inputs when tournament_match_id is present alongside both names
    (modals.py). This is the one place that shape is built, for both entry
    points (the Start draft button's picker and /premade_draft's match-thread
    context) -- if they ever built it separately and drifted apart, the
    typed-names bug this suppresses would silently come back for whichever
    one fell out of sync.
    """
    return {
        "tournament_match_id": match_id,
        "team_a_name": a_name,
        "team_b_name": b_name,
    }


def cube_picker_for_match(guild_id: int | None, match_id: int, a_name: str, b_name: str) -> discord.ui.View:
    """The premade cube picker, pre-named from the pairing.

    Both team names present is what makes CubeDraftModal drop its name inputs
    (modals.py), so the launcher cannot be given the wrong names.
    """
    from modals import CubeDraftSelectionView
    return CubeDraftSelectionView(
        session_type="premade",
        guild_id=guild_id,
        session_details_overrides=_picker_overrides(match_id, a_name, b_name),
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
    # roles=True is load-bearing: without it Discord renders the role mention
    # as a plain pill, notifies nobody, and adds nobody to the thread -- the
    # entire point of tagging both teams' roles in the body above.
    allowed_mentions = discord.AllowedMentions(everyone=False, roles=True, users=True)
    # Messageable.send's view overloads don't accept an explicit None (only a
    # real View or the omitted-argument default), even though py-cord treats
    # a falsy view as "no view" at runtime.
    if view is not None:
        message = await thread.send(content=body, view=view, allowed_mentions=allowed_mentions)
    else:
        message = await thread.send(content=body, allowed_mentions=allowed_mentions)
    await safe_pin(message)
    return message


async def create_match_room(message: discord.Message, match_id: int) -> discord.Thread | None:
    """Open a match's room off its pairing message: thread + pinned control message.

    Returns the thread, or None when Discord refuses (typically missing Manage
    Threads). A failure must not take the round's pairings down with it, so the
    caller simply leaves that match's line without a link.
    """
    async with db_session() as session:
        facts = await match_facts(session, match_id)
        if facts is None:
            return None
        match, a_name, b_name, label, draft = facts
        # match_facts already loaded both participants into this session, so
        # these are identity-map hits, not extra queries.
        part_a = await session.get(TournamentParticipant, match.team_a_participant_id)
        part_b = await session.get(TournamentParticipant, match.team_b_participant_id)
        body, view = control_body_and_view(
            match, a_name, b_name, label, draft,
            role_mentions=(part_a.role_id if part_a else None,
                           part_b.role_id if part_b else None),
        )

    try:
        thread = await message.create_thread(
            name=f"{a_name} vs {b_name}"[:DISCORD_THREAD_NAME_LIMIT],
            # Rooms are created when pairings post, and a match played midweek
            # must not have archived out of the sidebar.
            auto_archive_duration=THREAD_ARCHIVE_MAX_MINUTES,
        )
    except discord.HTTPException as e:
        logger.warning(f"Could not create a thread for match {match_id}: {e}")
        return None

    try:
        control = await _resolve_control_message(thread, None, body, view)
    except discord.HTTPException as e:
        logger.warning(
            f"Created a thread for match {match_id} but could not post its control "
            f"message ({e}); deleting the thread rather than leaving a dead room")
        try:
            await thread.delete()
        except discord.HTTPException:
            logger.warning(f"Could not delete the orphaned thread for match {match_id}")
        return None

    async with db_session() as session:
        stored = await session.get(TournamentMatch, match_id)
        if stored is not None:
            stored.thread_id = str(thread.id)
            stored.control_message_id = str(control.id)
    return thread


async def launch_block_for(match_id: int) -> str | None:
    """Why a new draft can't start for this match right now, or None.

    Opens its own session so any caller without facts of its own can ask.
    Callers who already have this match's facts (start_match_draft,
    match_room_context) should call block_for_facts directly instead, rather
    than paying for a second lookup here.

    A vanished match blocks rather than passing through: the creation-time
    guard in premade_session.py treats None here as "go ahead", and a
    dangling tournament_match_id has nothing downstream to catch it.
    """
    async with db_session() as session:
        facts = await match_facts(session, match_id)
        if facts is None:
            return "This match no longer exists."
        return block_for_facts(facts)


async def start_match_draft(interaction: discord.Interaction, match_id: int) -> None:
    """'Start draft' button: post the cube picker into the match thread."""
    async with db_session() as session:
        facts = await match_facts(session, match_id)
        if facts is None:
            await interaction.response.send_message(
                "This match no longer exists.", ephemeral=True)
            return
        match, a_name, b_name, label, draft = facts
        body, view = control_body_and_view(match, a_name, b_name, label, draft)
        # Same facts already fetched above -- launch_block_for would open a
        # second session and re-run the same lookup for nothing.
        block = block_for_facts(facts)

    if block is not None:
        # Re-render rather than only complaining: whatever changed underneath is
        # now visible to everyone, not just the clicker.
        await interaction.response.edit_message(content=body, view=view)
        await interaction.followup.send(block, ephemeral=True)
        return

    # Ephemeral: a picker that persists in the thread is both clutter and a
    # hazard -- a stale one stays clickable for its 180s view timeout and would
    # create a SECOND draft linked to this same match. The lobby it produces is
    # still public, because that is posted by the picker's own submit interaction.
    await interaction.response.send_message(
        content=(f"Pick a cube to start **{a_name}** vs **{b_name}** "
                 f"(the result records automatically when the draft finishes):"),
        view=cube_picker_for_match(interaction.guild_id, match_id, a_name, b_name),
        ephemeral=True,
    )


async def _refresh_pairing_message(
    bot: discord.Client, match: TournamentMatch, a_name: str, b_name: str
) -> None:
    """Re-render this match's line on the pairings message. No-op if unposted."""
    if not match.pairings_channel_id or not match.pairings_message_id:
        return
    channel = bot.get_channel(int(match.pairings_channel_id))
    if channel is None:
        return
    try:
        message = await as_messageable(channel).fetch_message(int(match.pairings_message_id))
        # view=None strips components: a message posted before this task's
        # switchover can still carry a ▶ Play button whose handler is gone, and
        # a bare content edit would leave that dead button in place. Messages
        # posted by the current code have no components, so this is a no-op
        # for them and a repair for legacy ones.
        await message.edit(content=render_pairing_line(
            a_name, b_name, match.thread_id,
            (match.team_a_wins, match.team_b_wins)), view=None)
    except discord.NotFound:
        logger.warning(f"Pairing message for match {match.id} is gone; not refreshed")
    except discord.HTTPException as e:
        logger.error(f"Failed to refresh pairing message for match {match.id}: {e}")


async def _refresh_match_views_with_facts(
    bot: discord.Client,
    match_id: int,
    facts: tuple[TournamentMatch, str, str, str, DraftSession | None] | None,
) -> None:
    """refresh_match_views's actual work, given facts the caller already fetched.

    No-op on the control message if it was never posted. The pairing refresh
    runs after and cannot prevent the control refresh that already happened.
    """
    if facts is None:
        return
    match, a_name, b_name, label, draft = facts
    body, view = control_body_and_view(match, a_name, b_name, label, draft)

    if match.control_message_id and match.thread_id:
        thread_id, control_id = int(match.thread_id), int(match.control_message_id)
        channel = bot.get_channel(thread_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(thread_id)
            except discord.HTTPException:
                logger.warning(f"Match {match_id} thread {thread_id} unreachable; not refreshed")
                channel = None
        if channel is not None:
            # The stored id always names a thread opened by create_match_room; the
            # wider return type here covers channel ids in general.
            thread = cast(discord.Thread, channel)
            try:
                message = await thread.fetch_message(control_id)
                await message.edit(content=body, view=view)
            except discord.NotFound:
                logger.warning(f"Match {match_id} control message {control_id} gone; not refreshed")
            except discord.HTTPException as e:
                logger.error(f"Failed to refresh control message for match {match_id}: {e}")

    await _refresh_pairing_message(bot, match, a_name, b_name)


async def refresh_match_views(bot: discord.Client, match_id: int) -> None:
    """Re-render a match's control message and its pairing line in place.

    Fetches its own facts. announce_and_refresh already holds this match's
    facts by the time it needs a refresh and calls the with-facts helper
    directly instead of coming through here.
    """
    async with db_session() as session:
        facts = await match_facts(session, match_id)
    await _refresh_match_views_with_facts(bot, match_id, facts)


async def safe_refresh_match_views(bot: discord.Client, match_id: int) -> None:
    """refresh_match_views, but a failure is logged and swallowed.

    Shared by every caller that triggers a refresh as a side effect of work
    that already succeeded (a result got recorded, a draft got cancelled,
    ...): a refresh failure must never break the flow that triggered it.
    """
    try:
        await refresh_match_views(bot, match_id)
    except Exception as e:
        logger.error(f"Failed to refresh control message for tournament match {match_id}: {e}")


async def match_room_context(
    session: AsyncSession, thread_id: int
) -> tuple[int, dict[str, Any], str | None] | None:
    """(match_id, overrides, block) for a match thread, or None if it isn't one.

    ``overrides`` are the session_details_overrides a draft launched here must
    carry — the same three the room's own Start draft button passes, which is
    what makes CubeDraftModal drop its team-name inputs. ``block`` is a
    user-facing reason a new draft can't start right now, or None.
    """
    match = (await session.execute(
        select(TournamentMatch).where(TournamentMatch.thread_id == str(thread_id))
    )).scalars().first()
    if match is None:
        return None
    facts = await match_facts(session, match.id)
    if facts is None:
        # The thread names a real match, but its round or a participant has
        # gone missing underneath it. Still return None -- a draft in a
        # broken room should still be creatable, unlinked -- but log it,
        # since the caller can't tell this apart from "not a match thread"
        # and the tournament data that would explain it is exactly what's gone.
        logger.warning(
            f"Match thread {thread_id} resolves to match {match.id}, but its "
            "facts are gone (missing round or participant); returning as an "
            "unlinked draft context")
        return None
    match, a_name, b_name, _label, _draft = facts
    overrides = _picker_overrides(match.id, a_name, b_name)
    block = block_for_facts(facts)
    return match.id, overrides, block


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
        _match, a_name, b_name, label, _draft = facts
        await channel.send(
            f"🔗 Linked to {label} — **{a_name}** vs **{b_name}**. "
            "The result will record automatically.")
    # Facts already fetched above -- the public refresh_match_views would
    # open a second session and fetch them again for the same match.
    await _refresh_match_views_with_facts(bot, match_id, facts)
