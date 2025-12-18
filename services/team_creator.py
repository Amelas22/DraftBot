"""
Team creation service for draft sessions.

Handles the complete flow of creating teams, generating display embeds,
and coordinating UI updates. Extracted from views.py to reduce coupling
and enable reuse across different team creation triggers.
"""

from loguru import logger
from datetime import datetime, timedelta
import discord
import random
from sqlalchemy import update, select

from session import AsyncSessionLocal, DraftSession, StakeInfo
from models.draft_session import DraftSession as DraftSessionModel
from utils import split_into_teams, generate_seating_order, get_formatted_stake_pairs, check_weekly_limits, add_links_to_embed_safely
from services.draft_setup_manager import DraftSetupManager
from services.state_manager import state_manager
from services.stake_service import calculate_and_store_stakes
from preference_service import get_players_bet_capping_preferences


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

                # Update session timing and stage
                session.teams_start_time = datetime.now()
                if session.session_type == 'premade':
                    session.deletion_time = datetime.now() + timedelta(days=7)
                else:
                    session.deletion_time = datetime.now() + timedelta(hours=4)
                session.session_stage = 'teams'

                # Create teams for random/test/staked/winston drafts
                if session.session_type in ['random', 'test', 'staked', 'winston']:
                    await split_into_teams(bot, session.session_id)
                    updated_session = await DraftSessionModel.get_by_session_id(draft_session_id)

                    # Clean up ready check data if it exists
                    if state_manager.get_ready_check_session(session_id):
                        logger.info(f"✅ Teams created - removing ready check data for session {session_id}")
                        state_manager.remove_ready_check_session(session_id)

                    # Calculate stakes for staked drafts
                    stake_info_by_player = {}
                    if persistent_view.session_type == "staked" and updated_session and updated_session.team_a and updated_session.team_b:
                        all_players = updated_session.team_a + updated_session.team_b

                        cap_info = await get_players_bet_capping_preferences(all_players, guild_id=guild_id)

                        await calculate_and_store_stakes(guild_id, updated_session, cap_info)

                        stake_stmt = select(StakeInfo).where(StakeInfo.session_id == session_id)
                        stake_results = await db_session.execute(stake_stmt)
                        stake_infos = stake_results.scalars().all()

                        for stake_info in stake_infos:
                            stake_info_by_player[stake_info.player_id] = stake_info

                    session = updated_session

                # Generate seating order based on session type
                if session.session_type != "swiss":
                    sign_ups_list = list(session.sign_ups.keys())
                    if session.session_type == "premade":
                        seating_order = await generate_seating_order(bot, session)

                        # Update sign_ups to match seating order
                        name_to_id = {name: user_id for user_id, name in session.sign_ups.items()}
                        new_sign_ups = {name_to_id[name]: name for name in seating_order}

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
                    seating_order = [session.sign_ups[user_id] for user_id in sign_ups_list]
                    new_sign_ups = {user_id: session.sign_ups[user_id] for user_id in sign_ups_list}
                    await db_session.execute(update(DraftSession)
                                        .where(DraftSession.session_id == session.session_id)
                                        .values(sign_ups=new_sign_ups))

                # Create main embed
                embed = await _create_teams_embed(session, team_a_display_names if session.session_type != 'swiss' else None,
                                           team_b_display_names if session.session_type != 'swiss' else None,
                                           seating_order, stake_info_by_player, persistent_view.session_type)

                # Create channel announcement embed
                channel_embed = _create_channel_announcement_embed(session, seating_order)

                # Handle staked drafts specially
                if persistent_view.session_type == "staked":
                    await _handle_staked_draft_completion(
                        interaction, db_session, session, embed, channel_embed,
                        persistent_view, draft_session_id, bot
                    )
                    return True

                # Update button states for non-staked drafts
                for item in persistent_view.children:
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
                await db_session.commit()

        # Update message and send announcement
        try:
            await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=persistent_view)
        except Exception as e:
            logger.error(f"Failed to update draft message: {e}")

        await interaction.channel.send(embed=channel_embed)

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


async def _create_teams_embed(session, team_a_names, team_b_names, seating_order, stake_info_by_player, session_type):
    """Create the main embed showing teams and seating order."""

    title_prefix = "Winston " if session.session_type == 'winston' else ""
    embed = discord.Embed(
        title=f"{title_prefix}Draft-{session.draft_id} is Ready!",
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
        user_links.append(f"**{display_name}**: [Draft Link]({personalized_link})")

    add_links_to_embed_safely(embed, user_links, "Your Personalized Draft Links")

    # Add team fields for non-swiss
    if session.session_type != 'swiss' and team_a_names and team_b_names:
        team_a_label = "🔴 Team Red" if session_type in ["random", "staked"] else session.team_a_name
        team_b_label = "🔵 Team Blue" if session_type in ["random", "staked"] else session.team_b_name

        embed.add_field(name=team_a_label, value="\n".join(team_a_names), inline=True)
        embed.add_field(name=team_b_label, value="\n".join(team_b_names), inline=True)

    embed.add_field(name="Seating Order", value=" -> ".join(seating_order), inline=False)

    # Add stakes for staked drafts
    if session_type == "staked" and stake_info_by_player:
        stake_lines, total_stakes = await get_formatted_stake_pairs(session.session_id, session.sign_ups)

        formatted_lines = []
        for line in stake_lines:
            parts = line.split(': ')
            names = parts[0].split(' vs ')
            formatted_lines.append(f"**{names[0]}** vs **{names[1]}**: {parts[1]}")

        if formatted_lines:
            add_links_to_embed_safely(embed, formatted_lines, f"Bets (Total: {total_stakes} tix)")

    return embed


def _create_channel_announcement_embed(session, seating_order):
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
        link_entry = f"**{display_name}**: [Draft Link]({personalized_link})"

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

    channel_embed.add_field(name="Seating Order", value=" -> ".join(seating_order), inline=False)

    return channel_embed


async def _handle_staked_draft_completion(interaction, db_session, session, embed, channel_embed,
                                         persistent_view, draft_session_id, bot):
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

            if manager.sio.connected:
                await manager.sio.emit('getUsers')
        else:
            logger.info(f"DraftSetupManager not found for {draft_session_id}")

    except Exception as e:
        logger.exception(f"Error updating draft manager: {e}")
