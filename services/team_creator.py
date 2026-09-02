"""
Team creation service for draft sessions.

Handles the complete flow of creating teams, generating display embeds,
and coordinating UI updates. Extracted from views.py to reduce coupling
and enable reuse across different team creation triggers.
"""

from loguru import logger
from datetime import datetime, timedelta

from services.draft_pool_service import match_pool
import discord
import random
from sqlalchemy import update, select

from session import AsyncSessionLocal, DraftSession, StakeInfo
from helpers.display_names import format_display_name, format_seating_order
from helpers.draft_footer import apply_draft_footer_from_session
from models.draft_session import DraftSession as DraftSessionModel
from utils import split_into_teams, generate_seating_order, reorder_sign_ups, get_formatted_stake_pairs, check_weekly_limits, add_links_to_embed_safely
from services.draft_setup_manager import DraftSetupManager
from services.state_manager import state_manager
from notification_service import send_teams_created_dms


async def create_and_display_teams(bot, draft_session_id, interaction, persistent_view):
    """
    Complete team creation flow for draft sessions.

    Args:
        bot: Discord bot instance
        draft_session_id: The draft session ID
        interaction: Discord interaction (for responses and context)
        persistent_view: The PersistentView instance (for button updates and helpers)

    Returns:
        bool: True if successful, False otherwise
    """
    session_id = draft_session_id
    guild_id = str(interaction.guild_id)

    try:
        logger.info(f"Create teams initiated for session {draft_session_id} of type {persistent_view.session_type}")

        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                stmt = select(DraftSession).where(DraftSession.session_id == session_id)
                result = await db_session.execute(stmt)
                session = result.scalars().first()

                if not session:
                    await interaction.followup.send("The draft session could not be found.", ephemeral=True)
                    return False

                if session.session_type == 'winston':
                    if len(session.sign_ups) != 2:
                        await interaction.followup.send("Winston draft requires exactly 2 players.")
                        return False
                elif len(session.sign_ups) % 2 != 0:
                    await interaction.followup.send("There must be an even number of players to fire.")
                    return False

                if session.session_type == 'premade':
                    # Drop team members who left (removed from sign_ups but still in
                    # team_a/team_b) so a stale id can't KeyError downstream, then
                    # require the teams to be non-empty and equal — an even total
                    # alone allows a lopsided split (e.g. 2-vs-0).
                    team_a = [uid for uid in (session.team_a or []) if uid in session.sign_ups]
                    team_b = [uid for uid in (session.team_b or []) if uid in session.sign_ups]
                    if not team_a or not team_b or len(team_a) != len(team_b):
                        await interaction.followup.send(
                            "Premade teams must be non-empty and equal size "
                            f"(currently {len(team_a)} vs {len(team_b)})."
                        )
                        return False
                    if team_a != list(session.team_a or []) or team_b != list(session.team_b or []):
                        session.team_a = team_a
                        session.team_b = team_b
                        await db_session.execute(update(DraftSession)
                                            .where(DraftSession.session_id == session.session_id)
                                            .values(team_a=team_a, team_b=team_b))

                # Update session timing and stage
                session.teams_start_time = datetime.now()
                if session.session_type == 'premade':
                    session.deletion_time = datetime.now() + timedelta(days=7)
                else:
                    session.deletion_time = datetime.now() + timedelta(hours=4)
                session.session_stage = 'teams'

                stake_info_by_player = {}

                # Clean up any active ready check, regardless of session type
                from ready_check import ReadyCheckSession
                rc_channel = bot.get_channel(int(session.draft_channel_id)) if session.draft_channel_id else None
                await ReadyCheckSession.cleanup(session_id, rc_channel)

                # Create teams for random/test/staked/winston drafts
                if session.session_type in ['random', 'test', 'staked', 'winston']:
                    await split_into_teams(bot, session.session_id)
                    updated_session = await DraftSessionModel.get_by_session_id(draft_session_id)


                    session = updated_session

                # Capture the sides now -- `session` carries team_a/team_b for
                # BOTH paths here (premade had them from creation; random and
                # staked have just had them written by split_into_teams). The
                # money itself moves AFTER this transaction commits; see below.
                staked_done = False
                pool_sides = None
                # STAKED only, matching utils.py's settlement gate exactly. Entries
                # can only arrive through the staked signup UI, and settlement is
                # inside a staked-only branch -- so matching any wider would level
                # a pool that nothing would ever pay out, stranding it.
                # Staked queues and premade drafts with a fixed entry fee: the
                # two kinds of draft that hold a pool, and the same pair
                # settlement pays out.
                if session.session_type == "staked" or session.entry_fee:
                    pool_sides = (str(session.guild_id), session.session_id,
                                  list(session.team_a or []), list(session.team_b or []))

                # Generate seating order based on session type
                if session.session_type != "swiss":
                    sign_ups_list = list(session.sign_ups.keys())
                    if session.session_type == "premade":
                        # (user_id, decorated_display_name) pairs. Reorder sign_ups by
                        # the user_id and keep the RAW stored names as values (the
                        # decorated display name is only for the embed) — mapping by
                        # name would KeyError on icon-decorated / markdown-escaped names.
                        seating_pairs = await generate_seating_order(bot, session)
                        new_sign_ups = reorder_sign_ups(session.sign_ups, [user_id for user_id, _ in seating_pairs])
                        seating_order = [name for _, name in seating_pairs]

                        await db_session.execute(update(DraftSession)
                                            .where(DraftSession.session_id == session.session_id)
                                            .values(sign_ups=new_sign_ups))
                        session.sign_ups = new_sign_ups
                    else:
                        seating_order = [session.sign_ups[user_id] for user_id in sign_ups_list]

                    team_a_display_names = [session.sign_ups[user_id] for user_id in session.team_a]
                    team_b_display_names = [session.sign_ups[user_id] for user_id in session.team_b]
                else:
                    sign_ups_list = list(session.sign_ups.keys())
                    random.shuffle(sign_ups_list)
                    new_sign_ups = reorder_sign_ups(session.sign_ups, sign_ups_list)
                    seating_order = list(new_sign_ups.values())
                    await db_session.execute(update(DraftSession)
                                        .where(DraftSession.session_id == session.session_id)
                                        .values(sign_ups=new_sign_ups))

                # Create main embed
                embed = await _create_teams_embed(session, team_a_display_names if session.session_type != 'swiss' else None,
                                           team_b_display_names if session.session_type != 'swiss' else None,
                                           seating_order, stake_info_by_player, persistent_view.session_type)

                # Create channel announcement embed
                channel_embed = await _create_channel_announcement_embed(
                    session, seating_order, stake_info_by_player, persistent_view.session_type
                )

                # Handle staked drafts specially
                if persistent_view.session_type == "staked":
                    await _handle_staked_draft_completion(
                        interaction, db_session, session, embed, channel_embed,
                        persistent_view, draft_session_id, bot, guild_id
                    )
                    # Do NOT return from inside the transaction: the pool has to
                    # be matched, and that money cannot move while this
                    # transaction holds the write lock. Fall out first.
                    staked_done = True

                # Update button states for non-staked drafts. Guarded because a
                # staked draft used to return before reaching here; falling out
                # of the transaction instead must not start running it.
                for item in ([] if staked_done else persistent_view.children):
                    if isinstance(item, discord.ui.Button):
                        if item.custom_id == f"create_rooms_pairings_{draft_session_id}":
                            # Keep disabled for Winston drafts as per original logic
                            if session.session_type == 'winston':
                                item.disabled = True
                            else:
                                item.disabled = False
                        elif item.custom_id == f"cancel_draft_{draft_session_id}":
                            item.disabled = False
                        else:
                            item.disabled = True
                if not staked_done:
                    # The staked completion handler commits internally, so the
                    # transaction is already closed by the time we get here.
                    await db_session.commit()

        # The book closes here, OUTSIDE the transaction above.
        #
        # wallet_service.pay opens its own connection and SQLite is single-writer,
        # so calling it while that transaction still holds the write lock makes
        # the inner write wait on a lock only the outer transaction can release.
        # It times out, the retry loop exhausts, and the whole team-creation
        # transaction rolls back -- leaving teams written by split_into_teams
        # (its own transaction) beside a session_stage that never advanced.
        if pool_sides is not None:
            await match_pool(*pool_sides)

        if staked_done:
            # A staked draft's own completion handler has already posted its
            # embeds; everything below is the non-staked announcement path.
            return True

        # Update message and send announcement
        try:
            await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=persistent_view)
        except Exception as e:
            logger.error(f"Failed to update draft message: {e}")

        await interaction.channel.send(embed=channel_embed)

        # Send DM notifications with draft links to users who have opted in
        await send_teams_created_dms(
            bot_or_client=interaction.client,
            draft_session=session,
            guild_id=guild_id,
            channel_id=str(interaction.channel.id),
            channel_name=interaction.channel.name,
            guild_name=interaction.guild.name
        )

        # Handle draft manager updates
        await _update_draft_manager(draft_session_id, bot, interaction.client)

        # Check weekly limits if applicable
        if session.tracked_draft and session.premade_match_id is not None:
            await check_weekly_limits(interaction, session.premade_match_id, session.session_type, session.session_id)

        return True

    except Exception as e:
        logger.exception(f"Error in create_and_display_teams: {e}")
        try:
            await interaction.followup.send(
                "An error occurred while creating teams. Please try again.",
                ephemeral=True
            )
        except:
            pass
        return False


async def _add_stake_info_to_embed(embed, session, stake_info_by_player):
    """Add formatted stake information to an embed if applicable."""
    if not stake_info_by_player:
        return

    stake_lines, total_stakes = await get_formatted_stake_pairs(session.session_id, session.sign_ups)

    formatted_lines = []
    for line in stake_lines:
        parts = line.split(': ')
        names = parts[0].split(' vs ')
        formatted_lines.append(f"**{names[0]}** vs **{names[1]}**: {parts[1]}")

    if formatted_lines:
        add_links_to_embed_safely(embed, formatted_lines, f"Bets (Total: {total_stakes} tix)")


async def _create_teams_embed(session, team_a_names, team_b_names, seating_order, stake_info_by_player, session_type):
    """Create the main embed showing teams and seating order."""

    title_prefix = "Winston " if session.session_type == 'winston' else ""
    embed = discord.Embed(
        title=f"{title_prefix}Draft is Ready!",
        description=f"**Chosen Cube: [{session.cube}]"
                    f"(https://cubecobra.com/cube/list/{session.cube})**\n\n"
                    "Host of Draftmancer must manually adjust seating as per below. \n**TURN OFF RANDOM SEATING SETTING IN DRAFTMANCER**"
                    "\n\n**AFTER THE DRAFT**, select Create Chat Rooms and Post Pairings"
                    "\nPairings will post in the created draft-chat room",
        color=discord.Color.dark_gold() if session.session_type == "swiss" else discord.Color.blue()
    )

    # Add personalized draft links
    user_links = []
    for user_id, display_name in session.sign_ups.items():
        personalized_link = session.get_draft_link_for_user(display_name)
        # Label stays RAW: Discord does not process backslash escapes inside a
        # markdown link label, so escaping here renders literal backslashes.
        user_links.append(f"[{display_name}]({personalized_link})")

    add_links_to_embed_safely(embed, user_links, "Your Personalized Draft Links")

    # Add team fields for non-swiss
    if session.session_type != 'swiss' and team_a_names and team_b_names:
        team_a_label = "🔴 Team Red" if session_type in ["random", "staked"] else session.team_a_name
        team_b_label = "🔵 Team Blue" if session_type in ["random", "staked"] else session.team_b_name

        embed.add_field(name=team_a_label,
                        value="\n".join(format_display_name(n) for n in team_a_names), inline=True)
        embed.add_field(name=team_b_label,
                        value="\n".join(format_display_name(n) for n in team_b_names), inline=True)

    embed.add_field(name="Seating Order", value=format_seating_order(seating_order), inline=False)

    # Add stakes for staked drafts
    if session_type == "staked":
        await _add_stake_info_to_embed(embed, session, stake_info_by_player)

    apply_draft_footer_from_session(embed, session)

    return embed


async def _create_channel_announcement_embed(session, seating_order, stake_info_by_player, session_type):
    """Create the channel announcement embed."""

    channel_embed = discord.Embed(
        title="Teams have been formed. Seating Order Below!",
        description=f"**Chosen Cube: [{session.cube}]"
                    f"(https://cubecobra.com/cube/list/{session.cube})**\n\n",
        color=discord.Color.dark_gold() if session.session_type == "swiss" else discord.Color.green()
    )

    # Add personalized links split by team
    team_a_links = []
    team_b_links = []

    for user_id, display_name in session.sign_ups.items():
        personalized_link = session.get_draft_link_for_user(display_name)
        # Raw label -- see the note in _create_teams_embed.
        link_entry = f"[{display_name}]({personalized_link})"

        if session.session_type == 'swiss':
            team_a_links.append(link_entry)
        else:
            if user_id in session.team_a:
                team_a_links.append(link_entry)
            elif user_id in session.team_b:
                team_b_links.append(link_entry)

    if team_a_links:
        team_name = "Team Red" if session.session_type in ["random", "staked"] else session.team_a_name
        team_name = team_name if team_name else "Team A"
        add_links_to_embed_safely(channel_embed, team_a_links, f"{team_name} Draft Links",
                                  "red" if session.session_type in ["random", "staked"] else "")

    if team_b_links:
        team_name = "Team Blue" if session.session_type in ["random", "staked"] else session.team_b_name
        team_name = team_name if team_name else "Team B"
        add_links_to_embed_safely(channel_embed, team_b_links, f"{team_name} Draft Links",
                                  "blue" if session.session_type in ["random", "staked"] else "")

    channel_embed.add_field(name="Seating Order", value=format_seating_order(seating_order), inline=False)

    # Add stakes for staked drafts
    if session_type == "staked":
        await _add_stake_info_to_embed(channel_embed, session, stake_info_by_player)

    apply_draft_footer_from_session(channel_embed, session)

    return channel_embed


async def _handle_staked_draft_completion(interaction, db_session, session, embed, channel_embed,
                                         persistent_view, draft_session_id, bot, guild_id):
    """Handle special completion flow for staked drafts."""
    from views import CallbackButton, StakeCalculationButton

    # Create view with stake calculation button
    stake_view = discord.ui.View(timeout=None)

    for item in persistent_view.children:
        if isinstance(item, discord.ui.Button):
            button_copy = CallbackButton(
                label=item.label,
                style=item.style,
                custom_id=item.custom_id,
                custom_callback=item.custom_callback
            )

            if item.custom_id == f"create_rooms_pairings_{draft_session_id}":
                button_copy.disabled = False
            elif item.custom_id == f"cancel_draft_{draft_session_id}":
                button_copy.disabled = False
            else:
                button_copy.disabled = True

            stake_view.add_item(button_copy)

    stake_view.add_item(StakeCalculationButton(session.session_id))

    try:
        await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=stake_view)
    except discord.errors.NotFound:
        logger.warning("Original draft message not found when updating for staked draft - likely deleted by automation.")
    except Exception as e:
        logger.error(f"Failed to update draft message: {e}")

    await interaction.channel.send(embed=channel_embed)
    await db_session.commit()

    # Send DM notifications with draft links to users who have opted in
    await send_teams_created_dms(
        bot_or_client=interaction.client,
        draft_session=session,
        guild_id=guild_id,
        channel_id=str(interaction.channel.id),
        channel_name=interaction.channel.name,
        guild_name=interaction.guild.name
    )

    # Update draft manager
    await _update_draft_manager(draft_session_id, bot, interaction.client)

    # Check weekly limits
    if session.tracked_draft and session.premade_match_id is not None:
        await check_weekly_limits(interaction, session.premade_match_id, session.session_type, session.session_id)


async def _update_draft_manager(draft_session_id, bot, client):
    """Update the DraftSetupManager after team creation."""
    try:
        manager = DraftSetupManager.get_active_manager(draft_session_id)

        if manager:
            logger.info(f"TEAMS CREATED: Found existing manager for session {draft_session_id}")
            logger.info(f"TEAMS CREATED: Manager state - Seating set: {manager.seating_order_set}, "
                       f"Users count: {manager.users_count}, Expected count: {manager.expected_user_count}")

            manager.set_bot_instance(client)
            logger.info(f"Set bot instance on manager to ensure Discord messaging works")

            logger.info("Check session from team creator service")
            await manager.check_session_stage_and_organize()

            if manager.socket_client.connected:
                await manager.socket_client.emit('getUsers')
        else:
            logger.info(f"DraftSetupManager not found for {draft_session_id}")

    except Exception as e:
        logger.exception(f"Error updating draft manager: {e}")
