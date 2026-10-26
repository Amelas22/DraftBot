"""Discord glue for a team draft's thread and its control message.

Top-level module, mirroring match_control_view.py, so the team-draft cog, the
draft cog and the premade session hook can all import it without an import
cycle through cogs/. The text itself is rendered by helpers/matchup.py, which
stays free of Discord and database imports.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Coroutine

import discord
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import scheduler_enabled
from database.db_session import db_session
from helpers.matchup import (
    OPEN,
    Booking,
    Roster,
    Side,
    assign_people,
    booking_announcement,
    claimed_state,
    launch_block,
    render_matchup_control,
    side_label,
)
from helpers.pin_helpers import safe_pin
from helpers.utils import (
    DISCORD_THREAD_NAME_LIMIT,
    THREAD_ARCHIVE_MAX_MINUTES,
    as_messageable,
    not_none,
)
from match_control_view import lobby_link
from models.draft_session import DraftSession
from models.team_draft import TeamDraft
from services.scheduler_links import issue_link
from services.team_draft import BookingChange, by_thread, set_room

if TYPE_CHECKING:
    from cube_views.pack_options import BaseCubeSelectionView

GONE = "This team draft no longer exists."
# The one answer every team-draft entry point gives in a server without the
# scheduler (board, wizard, /team_draft_board, Start draft, /premade_draft).
NOT_ENABLED = "Team drafts aren't turned on in this server."
DELETED_ROLE = "deleted role"
TIME_LINK_VIEW = "team_draft"  # the scheduler link view for one matchup (scheduler_team_draft_api.VIEW)
TIME_LINK_INTRO = ("See both sides' usual weeks side by side and book a time that suits "
                   "everyone. The link works for 7 days.")
TIME_LINK_FAILED = "Couldn't make your link just now. Try again in a minute."


@dataclass(frozen=True)
class TeamDraftFacts:
    id: int
    name: str
    a: Side
    b: Side
    booking: Booking
    state: str
    set_up_by: str
    lobby_link: str | None
    thread_id: str | None
    control_message_id: str | None
    guild_id: str


def _role_name(guild: discord.Guild | None, role_id: str) -> str:
    role = guild.get_role(int(role_id)) if guild is not None else None
    return role.name if role is not None else DELETED_ROLE


def _member_name(guild: discord.Guild | None, user_id: str) -> str:
    member = guild.get_member(int(user_id)) if guild is not None else None
    return member.display_name if member is not None else user_id


def _side(guild: discord.Guild | None, role_ids: list[str], user_ids: list[str]) -> Side:
    label = side_label([_role_name(guild, r) for r in role_ids],
                       [_member_name(guild, u) for u in user_ids])
    return Side(label=label, role_ids=tuple(role_ids), user_ids=tuple(user_ids))


def _ran(draft: DraftSession | None) -> bool:
    """Whether a saved draft got past its sign-up: teams made, or a later stage."""
    return draft is not None and (draft.teams_start_time is not None or draft.session_stage is not None)


async def team_draft_facts(
    session: AsyncSession, team_draft_id: int, guild: discord.Guild | None
) -> TeamDraftFacts | None:
    """Everything a team draft's control message renders from, or None if it's gone.

    Names come from the guild's cache (no Discord call), so a caller may hold
    the session while building these. The lobby is the claiming draft's.
    A claim whose draft has neither a lobby nor has run yet is SETTING_UP:
    a draft deleted unplayed frees its matchup (release_deleted_draft), so a
    claim is only ever left without its draft while that draft is being made.
    """
    row = await session.get(TeamDraft, team_draft_id)
    if row is None:
        return None
    draft = None
    if row.draft_session_id is not None:
        draft = (await session.execute(
            select(DraftSession).where(DraftSession.session_id == row.draft_session_id)
        )).scalars().first()
    link = lobby_link(draft)
    return TeamDraftFacts(
        id=row.id,
        name=row.name,
        a=_side(guild, list(row.side_a_roles or []), list(row.side_a_users or [])),
        b=_side(guild, list(row.side_b_roles or []), list(row.side_b_users or [])),
        booking=Booking(start_utc=row.booked_start_utc, by_name=row.booked_by_name),
        state=OPEN if row.draft_session_id is None else claimed_state(
            has_lobby=link is not None, ran=_ran(draft)),
        set_up_by=_member_name(guild, row.created_by),
        lobby_link=link,
        thread_id=row.thread_id,
        control_message_id=row.control_message_id,
        guild_id=row.guild_id,
    )


def _body_and_view(facts: TeamDraftFacts) -> tuple[str, "TeamDraftControlView"]:
    """The control message for these facts. The buttons stay once started:
    Start draft then answers with the lobby instead of a picker."""
    body = render_matchup_control(
        facts.a, facts.b, facts.booking, facts.state, facts.set_up_by, facts.lobby_link)
    return body, TeamDraftControlView(facts.id)


def _picker_overrides(facts: TeamDraftFacts) -> dict[str, Any]:
    """The session_details_overrides a draft launched for this matchup carries.

    Both names present is what makes CubeDraftModal drop its name inputs
    (modals.py); team_draft_id is what lets the draft claim the matchup.
    Built once here for both entry points (Start draft, /premade_draft).
    """
    return {
        "team_a_name": facts.a.label,
        "team_b_name": facts.b.label,
        "team_draft_id": facts.id,
    }


def _cube_picker(guild_id: int | None, facts: TeamDraftFacts) -> "BaseCubeSelectionView":
    from modals import CubeDraftSelectionView
    return CubeDraftSelectionView(
        session_type="premade",
        guild_id=guild_id,
        session_details_overrides=_picker_overrides(facts),
    )


Press = Callable[[discord.Interaction, int], Coroutine[Any, Any, None]]


class TeamDraftControlView(discord.ui.View):
    """Persistent buttons on a team draft's control message: Find a time, Start draft."""

    def __init__(self, team_draft_id: int) -> None:
        super().__init__(timeout=None)
        self._add(team_draft_id, "Find a time", discord.ButtonStyle.secondary,
                  f"team_draft_time:{team_draft_id}", find_a_time)
        self._add(team_draft_id, "Start draft", discord.ButtonStyle.primary,
                  f"team_draft_start:{team_draft_id}", start_team_draft)

    def _add(self, team_draft_id: int, label: str, style: discord.ButtonStyle,
             custom_id: str, press: Press) -> None:
        button: discord.ui.Button[Any] = discord.ui.Button(
            label=label, style=style, custom_id=custom_id)
        # discord.ui.Item.callback is declared on the class as an unbound method
        # (self, interaction), so pyrefly sees the attribute type as still
        # expecting `self` even though py-cord calls it bound at dispatch time.
        button.callback = self._make_callback(team_draft_id, press)  # pyrefly: ignore [bad-assignment]
        self.add_item(button)

    @staticmethod
    def _make_callback(
        team_draft_id: int, press: Press
    ) -> Callable[[discord.Interaction], Coroutine[Any, Any, None]]:
        async def callback(interaction: discord.Interaction) -> None:
            await press(interaction, team_draft_id)
        return callback


async def open_team_draft_room(
    channel: discord.TextChannel, team_draft_id: int
) -> discord.Thread | None:
    """Open a matchup's public thread with its pinned control message, tagging both sides.

    Returns the thread, or None when Discord refuses. A thread whose control
    message can't be posted is deleted rather than left as a dead room.
    """
    async with db_session() as session:
        facts = await team_draft_facts(session, team_draft_id, channel.guild)
    if facts is None:
        return None
    body, view = _body_and_view(facts)

    try:
        thread = await channel.create_thread(
            name=facts.name[:DISCORD_THREAD_NAME_LIMIT],
            type=discord.ChannelType.public_thread,
            # A matchup may be booked days out; the thread must not archive
            # out of the sidebar before then.
            auto_archive_duration=THREAD_ARCHIVE_MAX_MINUTES,
        )
    except discord.HTTPException as e:
        logger.warning(f"Could not create a thread for team draft {team_draft_id}: {e}")
        return None

    try:
        # roles=True is load-bearing: without it the role mentions notify
        # nobody and add nobody to the thread.
        control = await thread.send(
            content=body, view=view,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=True))
    except discord.HTTPException as e:
        logger.warning(
            f"Created a thread for team draft {team_draft_id} but could not post its "
            f"control message ({e}); deleting the thread rather than leaving a dead room")
        try:
            await thread.delete()
        except discord.HTTPException:
            logger.warning(f"Could not delete the orphaned thread for team draft {team_draft_id}")
        return None
    await safe_pin(control)

    async with db_session() as session:
        await set_room(session, team_draft_id, str(thread.id), str(control.id))
    return thread


async def _reach_thread(bot: discord.Client, thread_id: int) -> object | None:
    """The thread from the cache, else fetched; None (logged by the caller) if unreachable."""
    channel = bot.get_channel(thread_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(thread_id)
    except discord.HTTPException:
        return None


async def refresh_team_draft_control(bot: discord.Client, team_draft_id: int) -> None:
    """Re-render a matchup's control message in place. No-op if it was never posted."""
    async with db_session() as session:
        row = await session.get(TeamDraft, team_draft_id)
        if row is None:
            return
        guild = bot.get_guild(int(row.guild_id))
        facts = await team_draft_facts(session, team_draft_id, guild)
    if facts is None or not facts.thread_id or not facts.control_message_id:
        return
    body, view = _body_and_view(facts)

    thread_id, control_id = int(facts.thread_id), int(facts.control_message_id)
    channel = await _reach_thread(bot, thread_id)
    if channel is None:
        logger.warning(f"Team draft {team_draft_id} thread {thread_id} unreachable; not refreshed")
        return
    try:
        message = await as_messageable(channel).fetch_message(control_id)
        await message.edit(content=body, view=view)
    except discord.NotFound:
        logger.warning(f"Team draft {team_draft_id} control message {control_id} gone; not refreshed")
    except discord.HTTPException as e:
        logger.error(f"Failed to refresh control message for team draft {team_draft_id}: {e}")


async def refresh_freed_team_draft(team_draft_id: int) -> None:
    """Re-render a matchup whose draft was just deleted unplayed, so its control
    message offers Start draft again. Logged, never raised: the draft is
    already gone. The bot comes from bot_registry, as the delete is reached
    from paths (cancel, the queue reaper) that go through the pool service."""
    import bot_registry

    bot = bot_registry.get_bot()
    if bot is None:
        logger.warning(f"Team draft {team_draft_id} freed but no bot registered; not refreshed")
        return
    try:
        await refresh_team_draft_control(bot, team_draft_id)
    except Exception as e:
        logger.error(f"Failed to refresh control message for freed team draft {team_draft_id}: {e}")


async def announce_booking(bot: discord.Client, team_draft_id: int, change: BookingChange) -> None:
    """Tag both sides in the matchup's thread with a committed booking change,
    then re-render the control message. Discord errors are logged, never raised:
    the booking already stands."""
    async with db_session() as session:
        row = await session.get(TeamDraft, team_draft_id)
        if row is None:
            return
        guild = bot.get_guild(int(row.guild_id))
        facts = await team_draft_facts(session, team_draft_id, guild)
    if facts is None or not facts.thread_id:
        return
    content = booking_announcement(change.by_name, change.old, change.new, facts.a, facts.b)

    thread_id = int(facts.thread_id)
    channel = await _reach_thread(bot, thread_id)
    if channel is None:
        logger.warning(f"Team draft {team_draft_id} thread {thread_id} unreachable; booking not announced")
        return
    try:
        await as_messageable(channel).send(
            content=content,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=True))
    except discord.HTTPException as e:
        logger.warning(f"Could not announce the booking for team draft {team_draft_id}: {e}")
    await refresh_team_draft_control(bot, team_draft_id)


def _is_bot(guild: discord.Guild | None, user_id: str) -> bool:
    member = guild.get_member(int(user_id)) if guild is not None else None
    return member is not None and member.bot


def _role_members(guild: discord.Guild | None, side: Side) -> list[str]:
    """Current non-bot members of a side's roles; a deleted role contributes nobody."""
    found: list[str] = []
    for role_id in side.role_ids:
        role = guild.get_role(int(role_id)) if guild is not None else None
        if role is not None:
            found.extend(str(m.id) for m in role.members if not m.bot)
    return found


async def side_rosters(bot: discord.Client, guild_id: str, team_draft_id: int) -> tuple[Roster, Roster]:
    """Both sides' labels and people as the guild's cache has them now (no Discord call).

    Bots are left out; someone on both sides is counted once (assign_people).
    """
    guild = bot.get_guild(int(guild_id))
    async with db_session() as session:
        facts = await team_draft_facts(session, team_draft_id, guild)
    if facts is None or facts.guild_id != guild_id:
        return Roster("Side A", ()), Roster("Side B", ())
    a_people, b_people = assign_people(
        [u for u in facts.a.user_ids if not _is_bot(guild, u)], _role_members(guild, facts.a),
        [u for u in facts.b.user_ids if not _is_bot(guild, u)], _role_members(guild, facts.b),
    )
    return (
        Roster(facts.a.label, tuple((u, _member_name(guild, u)) for u in a_people)),
        Roster(facts.b.label, tuple((u, _member_name(guild, u)) for u in b_people)),
    )


async def find_a_time(interaction: discord.Interaction, team_draft_id: int) -> None:
    """Find a time button: the presser's own 7-day draftbot.fyi link to this matchup."""
    if interaction.guild_id is None or not scheduler_enabled(str(interaction.guild_id)):
        await interaction.response.send_message(NOT_ENABLED, ephemeral=True)
        return
    user_id = str(not_none(interaction.user).id)
    try:
        url = await issue_link(user_id, str(interaction.guild_id), TIME_LINK_VIEW,
                               datetime.now(timezone.utc), subject=str(team_draft_id))
    except Exception:
        logger.exception(f"Team draft {team_draft_id}: couldn't issue a link for {user_id}")
        await interaction.response.send_message(TIME_LINK_FAILED, ephemeral=True)
        return
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open Find a time", url=url))
    await interaction.response.send_message(TIME_LINK_INTRO, view=view, ephemeral=True)


async def start_team_draft(interaction: discord.Interaction, team_draft_id: int) -> None:
    """Start draft button: the pre-named cube picker, or why a draft can't start."""
    if interaction.guild_id is None or not scheduler_enabled(str(interaction.guild_id)):
        await interaction.response.send_message(NOT_ENABLED, ephemeral=True)
        return
    async with db_session() as session:
        facts = await team_draft_facts(session, team_draft_id, interaction.guild)
    if facts is None:
        await interaction.response.send_message(GONE, ephemeral=True)
        return

    block = launch_block(facts.state, facts.lobby_link)
    if block is not None:
        # Re-render rather than only complaining: whatever changed underneath
        # is now visible to everyone, not just the presser.
        body, view = _body_and_view(facts)
        await interaction.response.edit_message(content=body, view=view)
        await interaction.followup.send(block, ephemeral=True)
        return

    # Ephemeral, like the tournament room's picker: a picker left in the thread
    # would be clutter that anyone could press. The one-shot claim at draft
    # creation is what actually stops a second draft.
    picker = _cube_picker(interaction.guild_id, facts)
    await interaction.response.send_message(
        content=f"Pick a cube to start **{facts.a.label}** vs **{facts.b.label}**:",
        view=picker,
        ephemeral=True,
    )
    picker.mark_once_sent(interaction)


async def team_draft_room_context(
    session: AsyncSession, thread_id: int, guild: discord.Guild | None = None
) -> tuple[int, dict[str, Any], str | None] | None:
    """(team_draft_id, overrides, block) for a team draft thread, or None if it isn't one.

    Same shape as match_control_view.match_room_context. ``guild`` names the
    sides; without it a role reads as "deleted role" and a person as their id.
    """
    row = await by_thread(session, str(thread_id))
    if row is None:
        return None
    facts = await team_draft_facts(session, row.id, guild)
    if facts is None:
        return None
    return facts.id, _picker_overrides(facts), launch_block(facts.state, facts.lobby_link)


async def re_register_team_draft_views(bot: discord.Bot) -> None:
    """Re-attach every posted control message's buttons after a restart.

    Started matchups keep theirs too: Start draft then answers with the lobby.
    """
    async with db_session() as session:
        rows = (await session.execute(
            select(TeamDraft.id, TeamDraft.control_message_id)
            .where(TeamDraft.control_message_id.is_not(None))
        )).all()
    for team_draft_id, control_message_id in rows:
        bot.add_view(TeamDraftControlView(team_draft_id), message_id=int(control_message_id))
