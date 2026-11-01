"""What happens when a draft ends: rooms and pairings, then the log.

None of it needs a Draftmancer connection. The manager runs it when its socket
hears endDraft; a draft with no manager runs the same code from wherever it
learns the draft is over. The one end-of-draft step that does need the socket --
unlocking the log inside Draftmancer early -- stays with whoever holds one, and
reaches publish_draft_log as its `release_log` callback.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

import discord
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.orm import undefer

from config import is_test_mode
from database.db_session import AsyncSessionLocal, db_session
from helpers.digital_ocean_helper import DigitalOceanHelper
from helpers.draft_footer import apply_draft_footer
from helpers.magicprotools_helper import MagicProtoolsHelper
from helpers.team_names import BLUE, RED
from models.draft_session import DraftSession
from models.match import MatchResult
from services.card_lending_service import dispatch_lock, drop_uncollected
from services.draft_deck_assignment import assign_drafted_decks, loan_source
from services.draft_log_store import post_team_logs, supersedes
from services.library_panel import loan_changed

# When the public embed may publish, after the log is captured. Aligned with the
# Draftmancer unlock timer in production (DRAFT_LOG_UNLOCK_TIMER_MINUTES); test
# mode shortens it so an end-to-end draft publishes in seconds.
PUBLISH_DELAY_SECONDS = 30 if is_test_mode() else 180 * 60

DraftLog = dict[str, Any]


# --- the end of a draft ----------------------------------------------------

async def finish_draft(bot: discord.Client, session_id: str, log: DraftLog | None) -> None:
    """Rooms and pairings (at most once), then capture the log, post each team
    its pools, and hand drafters their decks. A missing log still gets rooms."""
    row = await DraftSession.get_by_session_id(session_id)
    if row is None:
        logger.warning(f"No draft session for {session_id}; nothing to finish")
        return
    guild = bot.get_guild(int(row.guild_id)) if row.guild_id else None
    channel = bot.get_channel(int(row.draft_channel_id)) if row.draft_channel_id else None
    if guild:
        await create_rooms_once(bot, guild, channel, session_id, row.session_type)
    else:
        logger.info(f"Could not find guild for {session_id}")
    if log:
        await take_draft_log(bot, session_id, log)
    else:
        logger.warning(f"No draft log available to capture for {session_id}")


async def create_rooms_once(bot: discord.Client, guild: discord.Guild, channel: Any,
                            session_id: str, session_type: str | None) -> None:
    """Build the draft's rooms and pairings unless someone else already has.

    The claim is a conditional write of rooms_created_at, so of any number of
    callers -- the manager's endDraft, a pushed end, the reconciler's pull --
    exactly one builds. create_rooms_pairings is told the claim is its own,
    since it refuses to run for a draft whose rooms_created_at is set.

    A failed build gives the claim back: rooms_created_at is what the "Create
    Rooms & Post Pairings" button and /mutiny read as "done", and the failure
    message below sends people to exactly that button.
    """
    from views import PersistentView

    # Never raises: the log still has to be captured after this, whatever
    # happened to the rooms.
    try:
        claim = await claim_rooms(session_id)
        if claim is None:
            logger.info(f"Rooms already claimed for session {session_id} - skipping creation")
            return
        created = await PersistentView.create_rooms_pairings(
            bot, guild, session_id, session_type=session_type, claim=claim)
        # Nothing to give back after a run whose post_pairings threw: it
        # committed its own rooms_created_at, so the rooms exist and the
        # release finds no claim of ours left.
        returned = (not created) and await release_rooms_claim(session_id, claim)
    except Exception:
        logger.opt(exception=True).error(f"Room creation failed for session {session_id}")
        return
    if not channel:
        return
    if created:
        await channel.send("Rooms and Pairings have been created!")
    elif returned:
        await channel.send("Failed to create rooms and pairings. Check logs for details.")


async def claim_rooms(session_id: str) -> datetime | None:
    """Stamp rooms_created_at if nobody has. The stamp is the claim, or None
    when the rooms were already claimed or built."""
    stamp = datetime.now()
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                update(DraftSession)
                .where(DraftSession.session_id == session_id,
                       DraftSession.rooms_created_at.is_(None))
                .values(rooms_created_at=stamp))
    return stamp if result.rowcount == 1 else None


async def release_rooms_claim(session_id: str, claim: datetime) -> bool:
    """Clear rooms_created_at if it is still exactly our claim. False when a
    finished build has stamped its own time over it."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                update(DraftSession)
                .where(DraftSession.session_id == session_id,
                       DraftSession.rooms_created_at == claim)
                .values(rooms_created_at=None))
    return result.rowcount == 1


async def take_draft_log(bot: discord.Client, session_id: str, log: DraftLog) -> None:
    """Capture the log, then post each team its own pools (private team channels)
    and hand each drafter their deck. The reconciler retries the latter two."""
    await capture_draft_log(session_id, log)
    try:
        await post_team_logs(session_id, bot)
    except Exception as e:
        logger.error(f"Eager team-pool post failed for {session_id}: {e}")
    # Its own try, and not folded into post_team_logs: posting pools
    # is all-or-nothing on team channels resolving, and whether a
    # player can borrow their deck has nothing to do with a room.
    try:
        await assign_drafted_decks(session_id)
    except Exception:
        # With the traceback: the body spans SQLAlchemy, config
        # reads and log parsing, so `{e}` alone gives an operator a
        # bare TypeError with no file and no line. A broad catch is
        # right here; a broad catch that also destroys the evidence
        # is not. Some drafters may have a deck and others not --
        # the reconciler re-runs and finishes the rest.
        logger.opt(exception=True).error(
            f"Deck assignment failed for {session_id}; it may "
            f"have assigned some drafters and not others")


# --- capture -----------------------------------------------------------------

async def capture_draft_log(session_id: str, log: DraftLog) -> bool:
    """Persist the draft log as soon as the draft ends, WITHOUT posting it.

    Saves the raw log to the DB (`draft_data`) and DigitalOcean Spaces (raw
    JSON via upload_draft_log), records pack first-picks, and stamps
    `logs_captured_at`. Does NOT post the Discord embed or set `data_received`
    -- that happens at publish.

    The raw log is always written to the DB so it is never lost (publish can
    post from `draft_data` even if Spaces failed). `logs_captured_at` /
    `pack_first_picks` are only set when the Spaces upload succeeds, so a
    failed upload stays retryable (logs_captured_at stays NULL -> reconnect
    re-captures). Idempotent: the same run's log delivered again is skipped.
    A LATER run's log -- the draft was stopped and restarted in the same
    room -- replaces an unpublished capture, along with everything derived
    from the old log; a published one is kept.
    """
    if not log:
        logger.warning(f"capture_draft_log called with no draft_data for {session_id}; skipping")
        return False
    try:
        # Idempotency guard, keyed on WHICH log was captured.
        async with db_session() as session:
            existing = (await session.execute(
                select(DraftSession)
                .options(undefer(DraftSession.draft_data))
                .filter(DraftSession.session_id == session_id)
            )).scalar_one_or_none()
            if existing is None:
                logger.warning(f"No draft session for {session_id}; skipping capture")
                return False
            if existing.logs_captured_at is not None:
                if not supersedes(log, existing.draft_data):
                    logger.info(f"Draft log already captured for {session_id}; skipping")
                    return True
                if existing.data_received:
                    logger.warning(
                        f"A later draft log arrived for {session_id} after its "
                        f"first was published; keeping the published one")
                    return True
                logger.warning(
                    f"Draft {session_id} ended again with a later log "
                    f"(started {log.get('time')}, had "
                    f"{(existing.draft_data or {}).get('time')}); replacing the capture")
            superseding = existing.logs_captured_at is not None
            session_type, cube_id = existing.session_type, existing.cube

        # Upload raw JSON to Spaces.
        object_key = await upload_draft_log(session_type, cube_id, log)

        # Replacing a capture deletes the old log's uncollected decks in the
        # same transaction, under the lending dispatch lock (see
        # drop_uncollected); a first capture holds nothing.
        dropped: list[Any] = []
        async with (dispatch_lock() if superseding else contextlib.nullcontext()), \
                db_session() as session:
            draft_session = (await session.execute(
                select(DraftSession).filter(DraftSession.session_id == session_id)
            )).scalar_one_or_none()
            if not draft_session:
                # Spaces upload may already have happened; only the DB write failed.
                logger.warning(f"No draft session for {session_id} (spaces_key={object_key})")
                return False
            if superseding:
                # Back to "uncaptured" first: everything derived from the
                # old log goes, and the stamps below are re-set only once
                # the new log's upload has succeeded. A failed upload then
                # leaves a NULL stamp for the reconciler to retry, exactly
                # as for a first capture -- keeping the old stamp would
                # read the next attempt as the same run and skip it.
                draft_session.logs_captured_at = None
                draft_session.unlock_at = None
                draft_session.spaces_object_key = None
                draft_session.pack_first_picks = None
                draft_session.team_logs_posted_at = None
                draft_session.drafttable_url = None
                dropped = await drop_uncollected(session, loan_source(session_id))

            # Always persist the raw log so the data is never lost.
            draft_session.draft_data = log

            # Only mark fully captured when Spaces succeeded, so a failed
            # upload stays retryable (logs_captured_at stays NULL).
            if object_key:
                discord_user_pack_picks: dict[str, dict[str, str]] = {}
                if draft_session.sign_ups:
                    discord_ids = list(draft_session.sign_ups.keys())
                    sorted_users = users_by_seat(log)
                    for idx, (draft_user_id, _) in enumerate(sorted_users):
                        if idx < len(discord_ids):
                            discord_user_pack_picks[discord_ids[idx]] = pack_first_picks(log, draft_user_id)
                draft_session.pack_first_picks = discord_user_pack_picks
                draft_session.logs_captured_at = datetime.now()
                draft_session.unlock_at = draft_session.logs_captured_at + timedelta(
                    seconds=PUBLISH_DELAY_SECONDS
                )
                draft_session.spaces_object_key = object_key

            await session.commit()

        if dropped:
            loan_changed(dropped[0])  # one sync per draft: the panel is per session
        logger.info(f"Captured draft log for {session_id} (spaces_key={object_key})")
        return bool(object_key)
    except Exception as e:
        logger.exception(f"Error capturing draft log for {session_id}: {e}")
        return False


async def upload_draft_log(session_type: str | None, cube_id: str | None, log: DraftLog) -> str | None:
    """Upload draft log data to DigitalOcean Spaces. Returns the object key
    (e.g. 'team/cube-ts-id.json') on success, or None on failure."""
    start_time = log.get("time")
    draft_id = log.get("sessionID")

    # Create DigitalOcean helper
    do_helper = DigitalOceanHelper()

    try:
        # Determine folder based on session type
        folder = "swiss" if session_type == "swiss" else "team"
        filename = f'{cube_id}-{start_time}-{draft_id}.json'

        # Upload the JSON data
        result = await do_helper.upload_json(log, folder, filename)

        if result.success:
            logger.info(f"Draft log data uploaded to DigitalOcean Space: {result.object_path}")
            return result.object_path
        else:
            logger.warning("Failed to upload draft log data to DigitalOcean Spaces")
            return None

    except Exception as e:
        logger.error(f"Error uploading to DigitalOcean Space: {e}")
        return None


def users_by_seat(log: DraftLog) -> list[tuple[str, dict[str, Any]]]:
    """The log's (Draftmancer user id, user) pairs in seat order -- the order
    sign_ups is matched against, to tell which drafter is which Discord user."""
    users: dict[str, dict[str, Any]] = log["users"]
    return sorted(users.items(), key=_seat_of)


def _seat_of(item: tuple[str, dict[str, Any]]) -> int:
    return item[1].get("seatNum", 999)


def pack_first_picks(log: DraftLog, user_id: str) -> dict[str, str]:
    """Extract the first pick card name for each pack for a specific user."""
    try:
        return MagicProtoolsHelper().get_pack_first_picks(log, user_id)
    except Exception as e:
        # In case of any error, return empty result
        logger.error(f"Error getting first picks: {e}")
        return {}


# --- publish -----------------------------------------------------------------

async def publish_draft_log(bot: discord.Client, session_id: str, release: bool = False, *,
                            release_log: Callable[[], Awaitable[None]] | None = None) -> bool:
    """Post the captured draft log to Discord and mark it published.

    Reads the already-captured `draft_data` from the DB (capture runs at
    draft-end), posts the MagicProTools embed, and sets `data_received`.
    Idempotent: a session already published (`data_received`) is skipped.

    When `release=True` (manual early release), `release_log` unlocks the log
    in Draftmancer first. It is the caller's because unlocking needs whatever
    connection the caller holds; without one, release only posts. The
    timer-driven path does not release (Draftmancer auto-unlocks on its own).
    """
    try:
        async with db_session() as session:
            # undefer: draft_data is deferred at the mapper (it is dead
            # weight for ~128 other queries), and this is one of the two
            # places that genuinely needs the log.
            draft_session = (await session.execute(
                select(DraftSession)
                .options(undefer(DraftSession.draft_data))
                .filter(DraftSession.session_id == session_id)
            )).scalar_one_or_none()
            if not draft_session:
                logger.warning(f"No draft session for {session_id}; cannot publish")
                return False
            if draft_session.data_received:
                logger.info(f"Draft log already published for {session_id}; skipping")
                return True
            draft_data = draft_session.draft_data
            table_url = draft_session.drafttable_url
            guild_id = draft_session.guild_id

        if not draft_data:
            logger.warning(f"No captured draft_data for {session_id}; nothing to publish")
            return False

        if release and release_log is not None:
            await release_log()

        # Every draft gets a full table page, built before the embed because
        # its URL goes inside; best-effort, since the embed is the
        # deliverable. The row is re-read (the one above is detached) and the
        # publish runs inside that session so its fields stay live -- holding
        # a connection for the seconds a Scryfall fetch, render and upload
        # take, which NullPool and WAL-mode SQLite make harmless.
        #
        # The URL is recorded as soon as the page is up, not with
        # data_received: a failed embed is retried every reconciler tick,
        # and each retry would otherwise build and upload the page again.
        # So a page is built once per draft, unless its upload itself fails.
        if not table_url:
            from services.draft_table_publisher import publish as publish_table
            async with db_session() as session:
                draft_session = (await session.execute(
                    select(DraftSession).filter(DraftSession.session_id == session_id)
                )).scalar_one_or_none()
                if draft_session and draft_session.drafttable_url:
                    table_url = draft_session.drafttable_url  # an overlapping publish got there first
                elif draft_session:
                    table_url = await publish_table(draft_data, draft_session)
                    if table_url:
                        draft_session.drafttable_url = table_url
                        await session.commit()

        # Post the MagicProTools embed/links to Discord.
        sent = False
        if guild_id:
            sent = await send_draft_log_embed(bot, session_id, draft_data, table_url=table_url)

        if not sent:
            logger.warning(
                f"MagicProTools embed not sent for {session_id}; "
                "leaving data_received unset so publish is retried"
            )
            return False

        async with db_session() as session:
            draft_session = (await session.execute(
                select(DraftSession).filter(DraftSession.session_id == session_id)
            )).scalar_one_or_none()
            if draft_session:
                draft_session.data_received = True
                await session.commit()

        logger.info(f"Published draft log for {session_id} (release={release})")
        return True
    except Exception as e:
        logger.exception(f"Error publishing draft log for {session_id}: {e}")
        return False


async def send_draft_log_embed(bot: discord.Client, session_id: str, draft_data: DraftLog,
                               table_url: str | None = None) -> bool:
    """Find the guild's draft-logs channel and send the embed if found.

    Returns:
        bool: True if the embed was actually sent to a channel, False if
        the guild/`draft-logs` channel wasn't found or an exception was
        caught. Callers (e.g. `publish_draft_log`) rely on this to decide
        whether the publish should be retried later.
    """
    try:
        row = await DraftSession.get_by_session_id(session_id)
        guild_id = row.guild_id if row else None
        guild = bot.get_guild(int(guild_id)) if guild_id else None
        if not guild:
            logger.warning(f"Could not find guild with ID {guild_id}")
            return False

        # Find a channel named "draft-logs"
        draft_logs_channel = None
        for channel in guild.channels:
            if channel.name.lower() == "draft-logs" and isinstance(channel, discord.abc.Messageable):
                draft_logs_channel = channel
                break

        if draft_logs_channel is None:
            logger.warning(f"No 'draft-logs' channel found in guild {guild.name}, skipping embed message")
            return False

        # Generate the embed and send it
        embed = await build_draft_log_embed(session_id, draft_data, table_url=table_url)
        message = await draft_logs_channel.send(embed=embed)
        logger.info(f"Sent MagicProTools links to #{draft_logs_channel.name} in {guild.name}")

        # Save the channel and message IDs to the database
        async with db_session() as session:
            # Get a fresh reference to the draft session
            stmt = select(DraftSession).filter(DraftSession.session_id == session_id)
            result = await session.execute(stmt)
            draft_session = result.scalar_one_or_none()

            if draft_session:
                draft_session.logs_channel_id = str(draft_logs_channel.id)
                draft_session.logs_message_id = str(message.id)
                await session.commit()
                logger.info(f"Saved logs channel and message IDs for session {session_id}")

                # Update victory messages to include the logs link
                # Import the function here to avoid circular imports
                from utils import check_and_post_victory_or_draw
                try:
                    await check_and_post_victory_or_draw(bot, session_id)
                    logger.info(f"Successfully updated victory messages with logs link for session {session_id}")
                except Exception as e:
                    logger.error(f"Error updating victory messages with logs link: {e}")
        return True
    except Exception as e:
        logger.error(f"Error sending MagicProTools embed: {e}")
        return False


def mpt_embed_field_value(direct_url: str | None) -> str:
    """Embed field value for one player's MagicProTools link, or an unavailable
    note when the direct API push returned no URL."""
    if direct_url:
        return f"[View on MagicProTools]({direct_url})"
    return "_log temporarily unavailable_"


async def build_draft_log_embed(session_id: str, draft_data: DraftLog,
                                table_url: str | None = None) -> discord.Embed:
    """Generate a Discord embed with MagicProTools links for all drafters"""
    friendly_id: str | None = None
    cube_id: str | None = None
    try:
        # Get the draft session to access sign_ups and start time
        async with db_session() as session:
            # Get draft session in this session context
            stmt = select(DraftSession).filter(DraftSession.session_id == session_id)
            result = await session.execute(stmt)
            draft_session = result.scalar_one_or_none()

            player_records: dict[str, dict[str, int]] = {}
            if not draft_session:
                logger.warning(f"Draft session not found for session ID: {session_id}")
                sign_ups: dict[str, Any] = {}
                formatted_start_time = "Unknown"
            else:
                friendly_id, cube_id = draft_session.friendly_id, draft_session.cube
                sign_ups = draft_session.sign_ups or {}
                if draft_session.teams_start_time:
                    start_time = draft_session.teams_start_time
                    # Format the start time for Discord
                    start_timestamp = int(start_time.timestamp())
                    formatted_start_time = f"<t:{start_timestamp}:F>"
                else:
                    formatted_start_time = "Unknown"

                # Get all match results for this session and calculate player records
                if draft_session.victory_message_id_draft_chat:  # Only fetch if victory message exists
                    match_results_stmt = select(MatchResult).filter(MatchResult.session_id == session_id)
                    match_results_result = await session.execute(match_results_stmt)
                    match_results = match_results_result.scalars().all()

                    # Calculate win-loss records for each player
                    for match in match_results:
                        # Skip matches without a winner
                        if not match.winner_id:
                            continue

                        # Add win for winner, loss for loser
                        if match.winner_id == match.player1_id:
                            # Player 1 won
                            player_records.setdefault(match.player1_id, {"wins": 0, "losses": 0})["wins"] += 1
                            player_records.setdefault(match.player2_id, {"wins": 0, "losses": 0})["losses"] += 1
                        elif match.winner_id == match.player2_id:
                            # Player 2 won
                            player_records.setdefault(match.player2_id, {"wins": 0, "losses": 0})["wins"] += 1
                            player_records.setdefault(match.player1_id, {"wins": 0, "losses": 0})["losses"] += 1

            embed = discord.Embed(
                title="Draft Log",
                description=f"View your draft in MagicProTools with the links below:\n\n**Draft Start:** {formatted_start_time}",
                color=0x3498db  # Blue color
            )
            apply_draft_footer(embed, friendly_id, cube_id)

            # Added before any per-player field so it reads as the headline.
            if table_url:
                embed.add_field(
                    name="📊 Full draft table",
                    value=f"[Replay every pack around the table]({table_url})",
                    inline=False,
                )

            # Get list of sign_ups keys (Discord user IDs) and values (display names or dictionaries)
            sign_up_discord_ids = list(sign_ups.keys())
            sign_up_display_names = list(sign_ups.values())

            # Create mapping of user index to Discord display name and ID
            # First sort users by seat number
            sorted_users = users_by_seat(draft_data)

            # Now map Discord display names and IDs to sorted users
            discord_name_by_user_id: dict[str, str] = {}
            discord_id_by_user_id: dict[str, str] = {}
            for idx, (user_id, _) in enumerate(sorted_users):
                if idx < len(sign_up_discord_ids):
                    discord_id_by_user_id[user_id] = sign_up_discord_ids[idx]

                    if isinstance(sign_up_display_names[idx], str):
                        discord_name_by_user_id[user_id] = sign_up_display_names[idx]
                    elif isinstance(sign_up_display_names[idx], dict) and 'name' in sign_up_display_names[idx]:
                        # Handle dictionary format
                        discord_name_by_user_id[user_id] = sign_up_display_names[idx]['name']

            # Dictionary to store MagicProTools links for each Discord ID
            magicprotools_links: dict[str, dict[str, str]] = {}
            mpt_helper = MagicProtoolsHelper()

            for idx, (user_id, user_data) in enumerate(sorted_users):
                user_name = user_data["userName"]

                # Get Discord display name if available
                discord_name = discord_name_by_user_id.get(user_id)
                discord_id = discord_id_by_user_id.get(user_id)

                # Add team color emoji based on player position. Seating
                # alternates, so an even seat is on the same side as team_a --
                # the emoji comes from the same constants every Discord surface
                # uses, so the Draftmancer seat list and the teams embed cannot
                # disagree about which colour a side is.
                team_emoji = RED.emoji if idx % 2 == 0 else BLUE.emoji

                # Get win-loss record if available
                record_str = ""
                trophy_emoji = ""
                if discord_id and discord_id in player_records:
                    record = player_records[discord_id]
                    record_str = f" ({record['wins']}-{record['losses']})"
                    # Add trophy emoji if they have 3 wins
                    if record['wins'] == 3:
                        trophy_emoji = "🏆 "

                # Format the name with team emoji, trophy, and record
                display_name = f"{team_emoji} {trophy_emoji}{user_name}"
                if discord_name:
                    display_name = f"{team_emoji} {user_name} - {discord_name}{record_str} {trophy_emoji}"

                logger.info(f"Getting MagicProTools URL for {user_name} (ID: {user_id})")
                direct_mpt_url = None
                try:
                    direct_mpt_url = await mpt_helper.submit_to_api(user_id, draft_data)
                except Exception as e:
                    logger.error(f"Error submitting to MagicProTools API for {user_name}: {e}")

                if not direct_mpt_url:
                    logger.warning(f"No MagicProTools URL for {user_name}; showing unavailable note")

                embed.add_field(
                    name=display_name,
                    value=mpt_embed_field_value(direct_mpt_url),
                    inline=False,
                )

                if direct_mpt_url and discord_id:
                    magicprotools_links[discord_id] = {
                        "name": discord_name_by_user_id.get(user_id, user_name),
                        "link": direct_mpt_url,
                    }
                elif not discord_id:
                    logger.warning(f"No Discord ID mapping for {user_name} (ID: {user_id})")

            # Update the database with the MagicProTools links
            if magicprotools_links and draft_session:
                try:
                    draft_session.magicprotools_links = magicprotools_links
                    session.add(draft_session)
                    await session.commit()
                    logger.info(f"Updated DraftSession with MagicProTools links for {len(magicprotools_links)} users")
                except Exception as e:
                    logger.error(f"Error saving MagicProTools links to database: {e}")

            return embed
    except Exception as e:
        logger.error(f"Error generating Discord embed: {e}")
        # Return a basic embed if there's an error
        fallback = discord.Embed(
            title="Draft Log",
            description="Error generating MagicProTools links. Check logs for details.",
            color=0xFF0000  # Red color
        )
        # Stamp the fallback too — a failed log is exactly when someone needs
        # to know which draft it belonged to.
        return apply_draft_footer(fallback, friendly_id, cube_id)
