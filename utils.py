import random
import discord
import asyncio
import pytz
from sqlalchemy import update, select, func, or_, desc, and_
from datetime import datetime, timedelta
from session import AsyncSessionLocal, get_draft_session, Challenge, PlayerLimit, DraftSession, MatchResult, PlayerStats, Match, Team, WeeklyLimit
from sqlalchemy.orm import selectinload, joinedload
from trueskill import Rating, rate_1vs1
from discord.ui import View
from league import ChallengeView



async def split_into_teams(bot, draft_session_id):
    # Fetch the current draft session to ensure it's up to date.
    draft_session = await get_draft_session(draft_session_id)
    if not draft_session:
        print("The draft session could not be found.")
        return
    
    # Check if there are any sign-ups to split into teams.
    sign_ups = draft_session.sign_ups
    if random.randint(1, 100) > 60 or draft_session.session_type == "test" or draft_session.session_type == "swiss":
        draft_session.true_skill_draft = False
    else:
        draft_session.true_skill_draft = True

    if sign_ups:
        sign_ups_list = list(sign_ups.keys())
        if draft_session.true_skill_draft:
            guild = bot.get_guild(int(draft_session.guild_id))
            team_a, team_b = await balance_teams(sign_ups_list, guild)
        else:
            random.shuffle(sign_ups_list)
            mid_point = len(sign_ups_list) // 2
            team_a = sign_ups_list[:mid_point]
            team_b = sign_ups_list[mid_point:]

        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                # Update the draft session with the new teams.
                await db_session.execute(update(DraftSession)
                                         .where(DraftSession.session_id == draft_session_id)
                                         .values(team_a=team_a, team_b=team_b, true_skill_draft=draft_session.true_skill_draft))
                await db_session.commit()


async def generate_seating_order(bot, draft_session, command_type=None):
    guild = bot.get_guild(int(draft_session.guild_id))

    team_a_members = [guild.get_member(int(user_id)) for user_id in draft_session.team_a]
    team_b_members = [guild.get_member(int(user_id)) for user_id in draft_session.team_b]

    random.shuffle(team_a_members)
    random.shuffle(team_b_members)

    seating_order = []
    for i in range(max(len(team_a_members), len(team_b_members))):
        if i < len(team_a_members) and team_a_members[i]:
            seating_order.append(team_a_members[i].display_name)
        if i < len(team_b_members) and team_b_members[i]:
            seating_order.append(team_b_members[i].display_name)


    return seating_order


async def calculate_pairings(session, db_session):
    if not session:
        print("Draft session not found.")
        return
    if session.session_type != "swiss":
        # num_players = len(session.team_a) + len(session.team_b)
        # if num_players not in [6, 8]:
        #     raise ValueError("Unsupported number of players. Only 6 or 8 players are supported.")

        assert len(session.team_a) == len(session.team_b), "Teams must be of equal size."
        
        for match_result in session.match_results[:]:
            db_session.delete(match_result)
        session.match_results = []

        # Generate pairings and create MatchResult instances
        for round_number in range(1, 4):
            for i, player_a in enumerate(session.team_a):
                player_b_index = (i + round_number - 1) % len(session.team_b)
                player_b = session.team_b[player_b_index]
                match_result = MatchResult(
                    session_id=session.session_id,
                    match_number=session.match_counter,
                    player1_id=player_a,
                    player1_wins=0,
                    player2_id=player_b,
                    player2_wins=0,
                    winner_id=None
                )
                db_session.add(match_result)
                session.match_counter += 1

    elif session.session_type == "swiss" and session.match_counter == 1:
        from tournament import Tournament
        to = Tournament(sign_ups=session.sign_ups)
        pairings = to.pair_round()
        for match in pairings:

            match_result = MatchResult(
                session_id=session.session_id,
                match_number=session.match_counter,
                player1_id=match[0],
                player1_wins=0,
                player2_id=match[1],
                player2_wins=0,
                winner_id=None
            )
            db_session.add(match_result)
            session.match_counter += 1
        state_to_save = to.get_state()
        return state_to_save, session.match_counter

    else:
        from tournament import Tournament
        to = Tournament(from_state=session.swiss_matches)
        stmt = select(MatchResult).where(MatchResult.session_id == session.session_id).order_by(MatchResult.match_number.desc()).limit(4)
        results = await db_session.execute(stmt)
        match_results = results.scalars().all()
        for match in match_results:
            winner_id = match.player1_id if match.player1_wins > match.player2_wins else match.player2_id
            to.record_match(player1_id=match.player1_id, player2_id=match.player2_id, winner_id=winner_id)
        match_counter = session.match_counter
        if match_counter > 12:
            return to.players
        elif match_counter < 12:
            pairings = to.pair_round()
            
            for match in pairings:
                match_result = MatchResult(
                    session_id=session.session_id,
                    match_number=match_counter,
                    player1_id=match[0],
                    player1_wins=0,
                    player2_id=match[1],
                    player2_wins=0,
                    winner_id=None
                )
                db_session.add(match_result)
                match_counter += 1
            
        state_to_save = to.get_state()
        return state_to_save, match_counter
          


async def post_pairings(bot, guild, session_id):
    async with AsyncSessionLocal() as db_session:
        # Fetch the draft session
        result = await db_session.execute(
            select(DraftSession).options(selectinload(DraftSession.match_results))
            .filter(DraftSession.session_id == session_id)
        )
        draft_session = result.scalar_one_or_none()

        if not draft_session:
            print("Draft session not found.")
            return
        if draft_session.session_type == "test":
            draft_chat_channel_obj = guild.get_channel(int(draft_session.draft_channel_id))
        else:
            draft_chat_channel_obj = guild.get_channel(int(draft_session.draft_chat_channel))
        if not draft_chat_channel_obj:
            print("Draft chat channel not found.")
            return
        if draft_session.session_type != "swiss":
            # Organize match results by round
            match_results_by_round = {}
            for match_result in draft_session.match_results:
                round_number = (match_result.match_number - 1) // (len(draft_session.team_a) or 1) + 1
                match_results_by_round.setdefault(round_number, []).append(match_result)

            for round_number, match_results in match_results_by_round.items():
                embed = discord.Embed(title=f"Round {round_number} Pairings", color=discord.Color.blue())
                from views import create_pairings_view  
                view = await create_pairings_view(bot, guild, session_id, match_results)

                for match_result in match_results:
                    player_name = guild.get_member(int(match_result.player1_id)).display_name if guild.get_member(int(match_result.player1_id)) else 'Unknown'
                    opponent_name = guild.get_member(int(match_result.player2_id)).display_name if guild.get_member(int(match_result.player2_id)) else 'Unknown'

                    # Formatting the pairings without wins
                    match_info = f"**Match {match_result.match_number}**\n{player_name} v.\n{opponent_name}"
                    embed.add_field(name="\u200b", value=match_info, inline=False)
                # Post the pairings message for the current round
                pairings_message = await draft_chat_channel_obj.send(embed=embed, view=view)

                # Update the pairing_message_id for each MatchResult in the round
                for match_result in match_results:
                    match_result.pairing_message_id = str(pairings_message.id)
                    db_session.add(match_result)
        else:
            round_number = (draft_session.match_counter - 1) // 4
            stmt = select(MatchResult).where(MatchResult.session_id == draft_session.session_id, MatchResult.match_number.between((draft_session.match_counter - 4), draft_session.match_counter)).order_by(MatchResult.match_number.asc())
            results = await db_session.execute(stmt)
            match_results = results.scalars().all()
            match_results_by_round = {}
            round_number = (draft_session.match_counter - 1) // 4
            for match_result in match_results:
                match_results_by_round.setdefault(round_number, []).append(match_result)
            for round_number, match_results in match_results_by_round.items():
                embed = discord.Embed(title=f"Round {round_number} Pairings", color=discord.Color.blue())
                from views import create_pairings_view  
                view = await create_pairings_view(bot, guild, session_id, match_results)
                
                for match_result in match_results:
                    player_name = guild.get_member(int(match_result.player1_id)).display_name if guild.get_member(int(match_result.player1_id)) else 'Unknown'
                    opponent_name = guild.get_member(int(match_result.player2_id)).display_name if guild.get_member(int(match_result.player2_id)) else 'Unknown'

                    # Formatting the pairings without wins
                    match_info = f"**Match {match_result.match_number}**\n{player_name} v.\n{opponent_name}"
                    embed.add_field(name="\u200b", value=match_info, inline=False)
                # Post the pairings message for the current round
                pairings_message = await draft_chat_channel_obj.send(embed=embed, view=view)

                # Update the pairing_message_id for each MatchResult in the round
                for match_result in match_results:
                    match_result.pairing_message_id = str(pairings_message.id)
                    db_session.add(match_result)
        # Commit the transaction to save the updates to the database
        await db_session.commit()


async def calculate_team_wins(draft_session_id):
    team_a_wins = 0
    team_b_wins = 0
    async with AsyncSessionLocal() as session:
        draft_session = await get_draft_session(draft_session_id)
        if draft_session:
            stmt = select(MatchResult).filter_by(session_id=draft_session_id)
            results = await session.execute(stmt)
            match_results = results.scalars().all()

            team_a_ids = set(draft_session.team_a)
            team_b_ids = set(draft_session.team_b)

            for result in match_results:
                if result.winner_id in team_a_ids:
                    team_a_wins += 1
                elif result.winner_id in team_b_ids:
                    team_b_wins += 1
        return team_a_wins, team_b_wins


async def generate_draft_summary_embed(bot, draft_session_id):
    async with AsyncSessionLocal() as session:
        draft_session = await get_draft_session(draft_session_id)
        if not draft_session:
            print("Draft session not found. Generate Draft Summary")
            return None

        guild = bot.get_guild(int(draft_session.guild_id))
        if draft_session.session_type != "swiss":
            team_a_names = [guild.get_member(int(user_id)).display_name for user_id in draft_session.team_a]
            team_b_names = [guild.get_member(int(user_id)).display_name for user_id in draft_session.team_b]

            team_a_wins, team_b_wins = await calculate_team_wins(draft_session_id)
            total_matches = draft_session.match_counter - 1
            half_matches = total_matches // 2
            title, description, discord_color = await determine_draft_outcome(bot, draft_session, team_a_wins, team_b_wins, half_matches, total_matches)

            embed = discord.Embed(title=title, description=description, color=discord_color)
            embed.add_field(name="Team A" if draft_session.session_type == "random" or draft_session.session_type == "test" else f"{draft_session.team_a_name}", value="\n".join(team_a_names), inline=True)
            embed.add_field(name="Team B" if draft_session.session_type == "random" or draft_session.session_type == "test" else f"{draft_session.team_b_name}", value="\n".join(team_b_names), inline=True)
            embed.add_field(
                name="**Draft Standings**", 
                value=(f"**Team A Wins:** {team_a_wins}" if draft_session.session_type == "random" or draft_session.session_type == "test" else f"**{draft_session.team_a_name} Wins:** {team_a_wins}") + 
                    (f"\n**Team B Wins:** {team_b_wins}" if draft_session.session_type == "random" or draft_session.session_type == "test" else f"\n**{draft_session.team_b_name} Wins:** {team_b_wins}"), 
                inline=False)
        else:
            sign_ups_list = list(draft_session.sign_ups.keys())
            title = f"Swiss Draft - Session {draft_session.draft_id}"
            description = f"Draft Start: <t:{int(draft_session.teams_start_time.timestamp())}:F>"
            discord_color = discord.Color.dark_magenta()
            embed = discord.Embed(title=title, description=description, color=discord_color)
            seating_order = [draft_session.sign_ups[user_id] for user_id in sign_ups_list]
            embed.add_field(name="Seating Order", value=" -> ".join(seating_order), inline=False)

        return embed

async def determine_draft_outcome(bot, draft_session, team_a_wins, team_b_wins, half_matches, total_matches):
    guild = bot.get_guild(int(draft_session.guild_id))
    if not guild:
        print("Guild not found")
        return "Error", "Guild not found"
    if team_a_wins > half_matches or team_b_wins > half_matches:
        winner_team_ids = draft_session.team_a if team_a_wins > team_b_wins else draft_session.team_b
        winner_team = [guild.get_member(int(member_id)) for member_id in winner_team_ids]

        if draft_session.session_type == "random":
            title = "Congratulations to " + ", ".join(member.display_name for member in winner_team if member) + " on winning the draft!"
            description = f"Draft Start: <t:{int(draft_session.teams_start_time.timestamp())}:F>"
            discord_color = discord.Color.gold()
        elif draft_session.session_type == "premade":
            team_name = draft_session.team_a_name if winner_team_ids == draft_session.team_a else draft_session.team_b_name
            title = f"{team_name} has won the match!"
            description = f"Congratulations to " + ", ".join(member.display_name for member in winner_team if member) + f" on winning the draft!\nDraft Start: <t:{int(draft_session.teams_start_time.timestamp())}:F>"
            discord_color = discord.Color.gold()
        else:
            title = "Draft Outcome"

    elif team_a_wins == 0 and team_b_wins == 0:
        title = f"Draft-{draft_session.draft_id} Standings" if draft_session.session_type == "random" or draft_session.session_type == "test" else f"{draft_session.team_a_name} vs. {draft_session.team_b_name}"
        description = "If a drafter is missing from this channel, they likely can still see the channel but have the Discord invisible setting on."
        discord_color = discord.Color.dark_blue()
    elif team_a_wins == half_matches and team_b_wins == half_matches and total_matches % 2 == 0:
        title = "The Draft is a Draw!"
        description = f"Draft Start: <t:{int(draft_session.draft_start_time.timestamp())}:F>"
        discord_color = discord.Color.light_grey()
    else:
        title = f"Draft-{draft_session.draft_id} Standings" if draft_session.session_type == "random" or draft_session.session_type == "test" else f"{draft_session.team_a_name} vs. {draft_session.team_b_name}"
        description = "If a drafter is missing from this channel, they likely can still see the channel but have the Discord invisible setting on."
        discord_color = discord.Color.dark_blue()
    return title, description, discord_color


async def fetch_match_details(bot, session_id: str, match_number: int):
    async with AsyncSessionLocal() as session:
        stmt = (
            select(DraftSession)
            .options(selectinload(DraftSession.match_results))
            .filter_by(session_id=session_id)
        )
        result = await session.execute(stmt)
        draft_session = result.scalars().first()
        if not draft_session:
            print(f"Draft session not found for session_id: {session_id}")
            return None, None  # Draft session not found

    match_result = next((match for match in draft_session.match_results if match.match_number == match_number), None)
    if not match_result:
        print(f"Match result not found within the session for match_number: {match_number}")
        return None, None  # Match not found within the session

    guild = bot.get_guild(int(draft_session.guild_id))
    if not guild:
        print("Guild not found.")
        return "Unknown Player", "Unknown Player"

    player1 = guild.get_member(int(match_result.player1_id))
    player2 = guild.get_member(int(match_result.player2_id))
    player1_name = player1.display_name if player1 else "Player Not Found"
    player2_name = player2.display_name if player2 else "Player Not Found"

    return player1_name, player2_name


async def update_draft_summary_message(bot, draft_session_id):
    async with AsyncSessionLocal() as session:
        draft_session = await get_draft_session(draft_session_id)
        if not draft_session:
            print("The draft session could not be found.")
            return
        if draft_session.session_type == "swiss":
            return
        updated_embed = await generate_draft_summary_embed(bot, draft_session_id)
        guild = bot.get_guild(int(draft_session.guild_id))
        channel = guild.get_channel(int(draft_session.draft_chat_channel))
        
        try:
            summary_message = await channel.fetch_message(int(draft_session.draft_summary_message_id))
            await summary_message.edit(embed=updated_embed)
        except Exception as e:
            print(f"Failed to update draft summary message: {e}")


async def check_and_post_victory_or_draw(bot, draft_session_id):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            draft_session = await get_draft_session(draft_session_id)
            if not draft_session:
                print("Draft session not found.")
                return

            guild = bot.get_guild(int(draft_session.guild_id))
            if not guild:
                print("Guild not found.")
                return
            
            if draft_session.session_type == "swiss":
                stmt = select(MatchResult).where(MatchResult.session_id == draft_session_id).order_by(MatchResult.match_number.desc()).limit(4)
                results = await session.execute(stmt)
                match_results = results.scalars().all()
                completed_matches = []
                for match in match_results:
                    if match.winner_id:
                        completed_matches.append(match.match_number)
                if len(completed_matches) == 4 and draft_session.match_counter > 12:
                    draft_chat_channel = guild.get_channel(int(draft_session.draft_chat_channel))
                    if draft_session.victory_message_id_draft_chat:
                        await draft_chat_channel.send("Results already posted for this draft")
                        return
                    else:    
                        players = await calculate_pairings(draft_session, session)
                        
                        # Sorting players by win points in descending order and then by name if tied
                        sorted_players = sorted(players.items(), key=lambda x: (-x[1]['win_points'], x[1]['display_name']))
                        
                        # Formatting the sorted list for display
                        standings = "\n".join(f"{idx + 1}. {player['display_name']} - {player['win_points']} wins" for idx, (_, player) in enumerate(sorted_players))
                        
                        sign_ups_list = list(draft_session.sign_ups.keys())
                        title = f"AlphaFrog Prelim Swiss Draft - Final Results"
                        description = f"Draft Start: <t:{int(draft_session.teams_start_time.timestamp())}:F>"
                        discord_color = discord.Color.gold()
                        embed = discord.Embed(title=title, description=description, color=discord_color)
                        seating_order = [draft_session.sign_ups[user_id] for user_id in sign_ups_list]
                        embed.add_field(name="Seating Order", value=" -> ".join(seating_order), inline=False)
                        embed.add_field(name="Standings", value=standings, inline=False)
                        
                        if draft_chat_channel:
                            await post_or_update_victory_message(session, draft_chat_channel, embed, draft_session, 'victory_message_id_draft_chat')

                        # Determine the correct results channel
                        results_channel_name = "team-draft-results" if draft_session.session_type == "random" else "league-draft-results"
                        results_channel = discord.utils.get(guild.text_channels, name=results_channel_name)
                        if results_channel:
                            await post_or_update_victory_message(session, results_channel, embed, draft_session, 'victory_message_id_results_channel')
                        pacific = pytz.timezone('US/Pacific')
                        utc = pytz.utc
                        pacific_time = utc.localize(draft_session.teams_start_time).astimezone(pacific)
                        midnight_pacific = pacific.localize(datetime(pacific_time.year, pacific_time.month, pacific_time.day))
                        start_of_week = midnight_pacific - timedelta(days=midnight_pacific.weekday())
                        for user_id, player in players.items():
                            # Query to find existing entry for the player this week
                            player_weekly_limit_stmt = select(PlayerLimit).where(
                                PlayerLimit.player_id == user_id,
                                PlayerLimit.WeekStartDate == start_of_week
                            )
                            player_weekly_limit_result = await session.execute(player_weekly_limit_stmt)
                            player_weekly_limit = player_weekly_limit_result.scalars().first()

                            if player_weekly_limit:
                                player_weekly_limit.drafts_participated += 1
                                if player_weekly_limit.drafts_participated == 1:
                                    player_weekly_limit.match_one_points = player['win_points']
                                elif player_weekly_limit.drafts_participated == 2:
                                    player_weekly_limit.match_two_points = player['win_points']
                                elif player_weekly_limit.drafts_participated == 3:
                                    player_weekly_limit.match_three_points = player['win_points']
                                elif player_weekly_limit.drafts_participated == 4:
                                    player_weekly_limit.match_four_points = player['win_points']
                            else:
                                # If not, create a new record
                                new_player_limit = PlayerLimit(
                                    player_id=user_id,
                                    display_name=player['display_name'],
                                    drafts_participated=1,
                                    WeekStartDate=start_of_week,
                                    match_one_points=player['win_points'],
                                    match_two_points=0,
                                    match_three_points=0,
                                    match_four_points=0
                                )
                                session.add(new_player_limit)
                                try:
                                    member = await guild.fetch_member(int(user_id))  # Fetch the member
                                    league_drafter_role = discord.utils.get(guild.roles, name='League Drafter')  # Get the role by name
                                    
                                    if league_drafter_role:
                                        if league_drafter_role not in member.roles:  # Check if the member does not already have the role
                                            await member.add_roles(league_drafter_role)
                                            print(f"Assigned 'League Drafter' role to {member.name}")
                                    else:
                                        print("Role 'League Drafter' not found in the guild.")
                                        
                                except discord.Forbidden:
                                    print("Permission error: Unable to assign roles. Check the bot's role position and permissions.")
                                except discord.HTTPException as e:
                                    print(f"HTTP exception occurred: {e}")

                        await session.commit()
                elif len(completed_matches) == 4:
                    state_to_save, match_counter = await calculate_pairings(draft_session, session)
                    await session.execute(update(DraftSession)
                        .where(DraftSession.session_id == draft_session.session_id)
                        .values(swiss_matches=state_to_save, match_counter=match_counter))
            
                    await session.commit()
                    await post_pairings(bot, guild, draft_session.session_id)

                return

            team_a_wins, team_b_wins = await calculate_team_wins(draft_session_id)
            total_matches = draft_session.match_counter - 1
            half_matches = total_matches // 2

            # Check victory or draw conditions
            if team_a_wins > half_matches or team_b_wins > half_matches or (team_a_wins == half_matches and team_b_wins == half_matches and total_matches % 2 == 0):
                if draft_session.tracked_draft and draft_session.premade_match_id is not None:
                    await update_match_db_with_wins_winner(draft_session.premade_match_id, team_a_wins, team_b_wins)
                gap = abs(team_a_wins - team_b_wins)

                embed = await generate_draft_summary_embed(bot, draft_session_id)
                three_zero_drafters = await calculate_three_zero_drafters(session, draft_session_id, guild)
                embed.add_field(name="3-0 Drafters", value=three_zero_drafters or "None", inline=False)
                
                # Handle the draft-chat channel message
                draft_chat_channel = guild.get_channel(int(draft_session.draft_chat_channel))
                if draft_chat_channel:
                    await post_or_update_victory_message(session, draft_chat_channel, embed, draft_session, 'victory_message_id_draft_chat')

                # Determine the correct results channel
                results_channel_name = "team-draft-results" if draft_session.session_type == "random" else "league-draft-results"
                results_channel = discord.utils.get(guild.text_channels, name=results_channel_name)
                if results_channel:
                    await post_or_update_victory_message(session, results_channel, embed, draft_session, 'victory_message_id_results_channel')
                else:
                    print(f"Results channel '{results_channel_name}' not found.")
                await session.execute(update(DraftSession)
                                         .where(DraftSession.session_id == draft_session_id)
                                         .values(winning_gap=gap))
                
                await session.commit()


async def post_or_update_victory_message(session, channel, embed, draft_session, victory_message_attr):
    if not channel:
        print("Channel not found.")
        return

    # Fetch the existing victory message ID from the draft_session
    victory_message_id = getattr(draft_session, victory_message_attr, None)
    if victory_message_id:
        try:
            message = await channel.fetch_message(int(victory_message_id))
            await message.edit(embed=embed)
        except discord.NotFound:
            print(f"Message ID {victory_message_id} not found in {channel.name}. Posting a new message.")
            victory_message_id = None

    if not victory_message_id:
        message = await channel.send(embed=embed)
        setattr(draft_session, victory_message_attr, str(message.id))
        session.add(draft_session)


async def calculate_three_zero_drafters(session, draft_session_id, guild):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            stmt = select(MatchResult).where(MatchResult.session_id == draft_session_id)
            results = await session.execute(stmt)
            match_results = results.scalars().all()

            # Count wins for each player
            win_counts = {}
            for match in match_results:
                winner_id = match.winner_id
                if winner_id:
                    win_counts[winner_id] = win_counts.get(winner_id, 0) + 1

            # Identify players with 3 wins
            three_zero_drafters = [player_id for player_id, win_count in win_counts.items() if win_count == 3]

            # Convert player IDs to names using the guild object
            three_zero_names = [guild.get_member(int(player_id)).display_name for player_id in three_zero_drafters if guild.get_member(int(player_id))]

            if three_zero_names:
                draft_session_stmt = select(DraftSession).where(DraftSession.session_id == draft_session_id)
                result = await session.execute(draft_session_stmt)
                draft_session = result.scalars().first()

                if draft_session:
                    # Update the pairings column with the list of three zero names
                    draft_session.trophy_drafters = three_zero_names
                else:
                    print("Draft session not found.")

            # Commit the transaction including the update to the draft session
            await session.commit()

            return ", ".join(three_zero_names)


async def cleanup_sessions_task(bot):
    while True:
        current_time = datetime.now()
        window_time = current_time - timedelta(hours=24)
        challenge_time = current_time - timedelta(hours=2)
        async with AsyncSessionLocal() as db_session:  
            async with db_session.begin():
                # Fetch sessions that are past their deletion time and in the deletion window
                stmt = select(DraftSession).where(DraftSession.deletion_time.between(window_time, current_time))
                results = await db_session.execute(stmt)
                sessions_to_cleanup = results.scalars().all()
                
                challenge_stmt = select(Challenge).where(Challenge.start_time < challenge_time)
                challenge_results = await db_session.execute(challenge_stmt)
                challenges_to_cleanup = challenge_results.scalars().all()

                for session in sessions_to_cleanup:
                    # Check if channel_ids is not None and is iterable before attempting to iterate
                    if session.channel_ids:
                        for channel_id in session.channel_ids:
                            channel = bot.get_channel(int(channel_id))
                            if channel:  # Check if channel was found
                                try:
                                    await channel.delete(reason="Session expired.")
                                except discord.NotFound:
                                    # If the message is not found, silently continue
                                    continue
                                except discord.HTTPException as e:
                                    print(f"Failed to delete channel: {channel.name}. Reason: {e}")

                    if session.draft_channel_id:
                        draft_channel = bot.get_channel(int(session.draft_channel_id))
                        if draft_channel and session.message_id:
                            try:
                                msg = await draft_channel.fetch_message(int(session.message_id))
                                await msg.delete()
                            except discord.NotFound:
                                # If the message is not found, silently continue
                                continue
                            except discord.HTTPException as e:
                                print(f"Failed to delete message ID {session.message_id} in draft channel. Reason: {e}")

                for challenge in challenges_to_cleanup:
                    if challenge.channel_id and challenge.message_id:
                        channel = bot.get_channel(int(challenge.channel_id))
                        if channel:
                            try:
                                msg = await channel.fetch_message(int(challenge.message_id))
                                await msg.delete()
                            except Exception as e:
                                print(f"Failed to delete challenge message {challenge.message_id}: {e}")

                    await db_session.delete(challenge)
                    # Commit deletion of challenge

                    print(f"{challenge.id} has been removed.")
        # Sleep for a certain amount of time before running again
        await asyncio.sleep(3600)  # Sleep for 1 hour

async def send_channel_reminders(bot, session_id):
    async with AsyncSessionLocal() as db_session:
        async with db_session.begin():
            stmt = select(DraftSession).where(DraftSession.session_id == session_id)
            result = await db_session.execute(stmt)
            session = result.scalars().first()
    if session.draft_start_time.tzinfo is None:
        draft_start_time = pytz.utc.localize(session.draft_start_time)
    
    # Calculate the reminder time (15 minutes before the draft start time)
    reminder_time = draft_start_time - timedelta(minutes=15)
    current_time = datetime.now(pytz.utc)  # Current time in UTC
    wait_seconds = (reminder_time - current_time).total_seconds()
    print(wait_seconds)
    # Wait until the reminder time
    if wait_seconds > 0:
        await asyncio.sleep(wait_seconds)
        
    async with AsyncSessionLocal() as db_session:
        async with db_session.begin():
            stmt = select(DraftSession).where(DraftSession.session_id == session_id)
            result = await db_session.execute(stmt)
            session = result.scalars().first()
    # Format the mention string and construct the reminder message
    mentions = " ".join([f"<@{user_id}>" for user_id in session.sign_ups])
    reminder_message = f"{mentions}\nReminder: Your draft starts in 15 minutes! Join here: {session.draft_link}"

    # Fetch the channel and send the reminder
    guild = bot.get_guild(int(session.guild_id))
    if guild:
        channel = guild.get_channel(int(session.draft_channel_id))
        if channel:
            try:
                await channel.send(reminder_message)
            except Exception as e:
                print(f"Failed to send reminder in channel {channel.name}: {e}")

async def update_player_stats_for_draft(session_id, guild):
    async with AsyncSessionLocal() as db_session: 
        async with db_session.begin():
            stmt = select(DraftSession).where(DraftSession.session_id == session_id)
            draft_session = await db_session.scalar(stmt)
            if not draft_session:
                print("Draft session not found.")
                return
            
            player_ids = draft_session.team_a + draft_session.team_b  # Combine both teams' player IDs
            
            for player_id in player_ids:
                stmt = select(PlayerStats).where(PlayerStats.player_id == player_id)
                player_stat = await db_session.scalar(stmt)
                
                if player_stat:
                    # Update existing player stats
                    player_stat.drafts_participated += 1
                else:
                    # Create new player stats record
                    player_stat = PlayerStats(
                        player_id=player_id,
                        drafts_participated=1,
                        games_won=0,
                        games_lost=0,
                        elo_rating=1200,  # Default ELO score
                        display_name=guild.get_member(int(player_id)).display_name if guild.get_member(int(player_id)) else "Unknown"
                    )
                    db_session.add(player_stat)

            await db_session.commit()


async def update_match_db_with_wins_winner(match_id, team_a_wins, team_b_wins):

    async with AsyncSessionLocal() as db_session: 
        async with db_session.begin():
            stmt = select(Match).where(Match.MatchID == match_id)
            match = await db_session.scalar(stmt)
            if not match:
                print("Match not found.")
                return
            
            pacific = pytz.timezone('US/Pacific')
            utc = pytz.utc
            # Convert UTC MatchDate to Pacific time and set time to midnight
            pacific_time = utc.localize(match.MatchDate).astimezone(pacific)
            midnight_pacific = pacific.localize(datetime(pacific_time.year, pacific_time.month, pacific_time.day))

            # Calculate the start of the week
            start_of_week = midnight_pacific - timedelta(days=midnight_pacific.weekday())


            initial_winner = match.DraftWinnerID
            new_winner = match.TeamAID if team_a_wins > team_b_wins else match.TeamBID if team_a_wins < team_b_wins else None
            
            match.DraftWinnerID = new_winner
            match.TeamAWins = team_a_wins
            match.TeamBWins = team_b_wins
            
            teams_to_update = []
            if initial_winner != new_winner:
                if initial_winner is None:
                    teams_to_update = [match.TeamAID, match.TeamBID]
                
                # Update Team MatchesCompleted
                for team_id in teams_to_update:
                    team_stmt = select(Team).where(Team.TeamID == team_id)
                    team = await db_session.scalar(team_stmt)
                    if not team:
                        print(f"Team ID {team_id} not found.")
                        continue
                    
                    team.MatchesCompleted += 1  # Increment matches completed for both teams on first determination
                    
                if new_winner:
                    # Update winning team
                    winner_team_stmt = select(Team).where(Team.TeamID == new_winner)
                    winner_team = await db_session.scalar(winner_team_stmt)
                    if winner_team:
                        winner_team.MatchWins += 1  # Increment match wins for the new winner
                        winner_team.PointsEarned += 1
                    if initial_winner:
                        # Decrement wins from initially incorrect winner
                        initial_winner_team_stmt = select(Team).where(Team.TeamID == initial_winner)
                        initial_winner_team = await db_session.scalar(initial_winner_team_stmt)
                        if initial_winner_team:
                            initial_winner_team.MatchWins -= 1  # Correct match wins if initial winner was wrong
                            initial_winner_team.PointsEarned -= 1
                # Update WeeklyLimit records
                if teams_to_update:
                    for team_id in teams_to_update:
                        team_stmt = select(Team).where(Team.TeamID == team_id)
                        team_update = await db_session.scalar(team_stmt)
                        team = int(team_id)
                        team_name = team_update.TeamName
                        weekly_limit = await get_or_create_weekly_limit(db_session, team, team_name, start_of_week)
                        weekly_limit.MatchesPlayed += 1  # Increment matches played for both teams on first determination
                        
                if new_winner:
                    team_stmt = select(Team).where(Team.TeamID == new_winner)
                    team_update = await db_session.scalar(team_stmt)
                    team = int(new_winner)
                    team_name = team_update.TeamName
                    winner_weekly_limit = await get_or_create_weekly_limit(db_session, team, team_name, start_of_week)
                    winner_weekly_limit.PointsEarned += 1  # Increment points for the winner
                    
                    if initial_winner:
                        team_stmt = select(Team).where(Team.TeamID == initial_winner)
                        team_update = await db_session.scalar(team_stmt)
                        team_name = team_update.TeamName
                        # Correct points if initially incorrect winner was declared
                        initial_winner_weekly_limit = await get_or_create_weekly_limit(db_session, initial_winner, team_name, start_of_week)
                        initial_winner_weekly_limit.PointsEarned -= 1

                await db_session.commit()


async def get_or_create_weekly_limit(db_session, team_id, team_name, start_of_week):
    # Try to find an existing WeeklyLimit for the team and current week
    start_of_week_date = start_of_week.date() if isinstance(start_of_week, datetime) else start_of_week
    weekly_limit_stmt = select(WeeklyLimit).where(
        WeeklyLimit.TeamID == int(team_id),
        func.date(WeeklyLimit.WeekStartDate) == start_of_week_date
    )
    weekly_limit = await db_session.scalar(weekly_limit_stmt)

    # If it does not exist, create a new one
    if not weekly_limit:
        weekly_limit = WeeklyLimit(
            TeamID=int(team_id),
            TeamName=team_name,
            WeekStartDate=start_of_week,
            MatchesPlayed=0,
            PointsEarned=0
        )
        db_session.add(weekly_limit)
        await db_session.flush()  # Ensure weekly_limit gets an ID assigned before returning

    return weekly_limit

async def check_weekly_limits(interaction, match_id, session_type=None, session_id=None):
    
    limit_messages = []
    if session_type != "swiss":
        async with AsyncSessionLocal() as db_session: 
            async with db_session.begin():
                stmt = select(Match).where(Match.MatchID == match_id)
                match = await db_session.scalar(stmt)
                if not match:
                    print("Match not found.")
                    return
                pacific = pytz.timezone('US/Pacific')
                utc = pytz.utc
                pacific_time = utc.localize(match.MatchDate).astimezone(pacific)
                midnight_pacific = pacific.localize(datetime(pacific_time.year, pacific_time.month, pacific_time.day))
                start_of_week = midnight_pacific - timedelta(days=midnight_pacific.weekday())
                teams_to_check = [match.TeamAID, match.TeamBID]
                for team_id in teams_to_check:
                    team_weekly_limit_stmt = select(WeeklyLimit).where(
                        WeeklyLimit.TeamID == team_id,
                        WeeklyLimit.WeekStartDate == start_of_week
                    )
                    team_weekly_limit = await db_session.scalar(team_weekly_limit_stmt)

                    if team_weekly_limit and (team_weekly_limit.MatchesPlayed >= 5 or team_weekly_limit.PointsEarned >= 3):
                        # Update DraftSession to untrack match
                        await db_session.execute(
                            update(DraftSession).
                            where(DraftSession.premade_match_id == str(match_id)).
                            values(tracked_draft=False)
                        )

                        condition = "matches" if team_weekly_limit.MatchesPlayed >= 5 else "points"
                        limit_messages.append(f"{team_weekly_limit.TeamName} has exceeded the weekly limit for {condition}. This match will not be tracked.")

                await db_session.commit()
    else:
        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                query = select(DraftSession).filter_by(session_id=session_id)
                result = await db_session.execute(query)
                draft_session = result.scalars().first()
                pacific = pytz.timezone('US/Pacific')
                utc = pytz.utc
                pacific_time = utc.localize(draft_session.teams_start_time).astimezone(pacific)
                midnight_pacific = pacific.localize(datetime(pacific_time.year, pacific_time.month, pacific_time.day))
                start_of_week = midnight_pacific - timedelta(days=midnight_pacific.weekday())
                if draft_session:
                    sign_ups = draft_session.sign_ups  
                    for user_id, display_name in sign_ups.items():
                        player_id = str(user_id)
                        # Query to find existing entry for the player this week
                        player_weekly_limit_stmt = select(PlayerLimit).where(
                                PlayerLimit.player_id == player_id,
                                PlayerLimit.WeekStartDate == start_of_week
                        )
                        player_weekly_limit_result = await db_session.execute(player_weekly_limit_stmt)
                        player_weekly_limit = player_weekly_limit_result.scalars().first()

                        if player_weekly_limit:
                            if player_weekly_limit.drafts_participated > 4:
                                condition = "matches" if player_weekly_limit.drafts_participated else "points"
                                limit_messages.append(f"{player_weekly_limit.display_name} has exceeded the weekly limit for {condition}. This match will not be tracked.")

                    # Commit the changes to the database
                    await db_session.commit()
                    
    if limit_messages:
        await interaction.followup.send("\n".join(limit_messages))
    else:
        # If no limits are exceeded, proceed with your draft creation or next steps here.
        pass


async def update_player_stats_and_elo(match_result):
    async with AsyncSessionLocal() as session:
        async with session.begin():
            player1 = await session.get(PlayerStats, match_result.player1_id)
            player2 = await session.get(PlayerStats, match_result.player2_id)

            if match_result.winner_id:
                # Determine winner and loser based on match_result
                if match_result.winner_id == match_result.player1_id:
                    winner, loser = player1, player2
                else:
                    winner, loser = player2, player1

                # Update ELO ratings
                elo_diff = calculate_elo_diff(winner.elo_rating, loser.elo_rating)
                winner.elo_rating += elo_diff
                loser.elo_rating -= elo_diff

                # Create TrueSkill Rating objects for winner and loser
                winner_rating = Rating(mu=winner.true_skill_mu, sigma=winner.true_skill_sigma)
                loser_rating = Rating(mu=loser.true_skill_mu, sigma=loser.true_skill_sigma)

                # Update TrueSkill ratings based on the match outcome
                new_winner_rating, new_loser_rating = rate_1vs1(winner_rating, loser_rating)

                # Update player stats with new TrueSkill ratings
                winner.true_skill_mu = new_winner_rating.mu
                loser.true_skill_mu = new_loser_rating.mu
                winner.true_skill_sigma = new_winner_rating.sigma
                loser.true_skill_sigma = new_loser_rating.sigma

                # Update games won and lost
                winner.games_won += 1
                loser.games_lost += 1

                await session.commit()

def calculate_elo_diff(winner_elo, loser_elo, k=20):
    """Calculate Elo rating difference after a game."""
    expected_win = 1 / (1 + 10 ** ((winner_elo - loser_elo) / 400))
    return k * (1 - expected_win)

async def balance_teams(player_ids, guild):
    async with AsyncSessionLocal() as db_session:
        # Ensure PlayerStats for each player
        for player_id in player_ids:
            player_stat = await db_session.get(PlayerStats, player_id)
            if not player_stat:
                player_stat = PlayerStats(
                    player_id=player_id,
                    drafts_participated=0,
                    games_won=0,
                    games_lost=0,
                    display_name=guild.get_member(int(player_id)).display_name if guild.get_member(int(player_id)) else "Unknown",
                    elo_rating=1200,
                )
                db_session.add(player_stat)
        await db_session.commit()

        stmt = select(PlayerStats).where(
            PlayerStats.player_id.in_(player_ids)
        ).order_by(
            desc(PlayerStats.true_skill_mu - (2 * PlayerStats.true_skill_sigma))
        )
        result = await db_session.execute(stmt)
        ordered_players = result.scalars().all()

        team_a, team_b = [], []

        is_team_a_turn = True

        for index, player_stat in enumerate(ordered_players):
            if index == 0:
                # Assign the first player to team A
                team_a.append(player_stat.player_id)
            elif is_team_a_turn:
                # Next two players go to team B
                team_b.append(player_stat.player_id)
                if len(team_b) % 2 == 0:  # Check if it's time to switch back to team A
                    is_team_a_turn = False
            else:
                # Following two players back to team A
                team_a.append(player_stat.player_id)
                if len(team_a) % 2 == 1:  # Check if it's time to switch back to team B
                    is_team_a_turn = True

    return team_a, team_b

async def re_register_challenges(bot):
    async with AsyncSessionLocal() as db_session:
        async with db_session.begin():
            from session import SwissChallenge
            stmt = select(SwissChallenge)
            result = await db_session.execute(stmt)
            challenge_to_update = result.scalars().all()

            for challenge in challenge_to_update:
                if challenge.channel_id:
                    channel_id = int(challenge.channel_id)
                    channel = bot.get_channel(channel_id)
                    if channel:
                        try:
                            message = await channel.fetch_message(int(challenge.message_id))
                            view = ChallengeView(challenge_id=challenge.id, 
                                                 command_type="swiss"
                            )
                            await message.edit(view=view)
                        except discord.NotFound:
                            # Handle cases where the message or channel might have been deleted
                            print(f"Message or channel not found for session: {challenge.id}")
                        except Exception as e:
                            # Log or handle any other exceptions
                            print(f"Failed to re-register view for challenge: {challenge.id}, error: {e}")

async def re_register_views(bot):
    async with AsyncSessionLocal() as db_session:
        async with db_session.begin():
            stmt = select(DraftSession).options(joinedload(DraftSession.match_results)).order_by(desc(DraftSession.id)).limit(10)
            result = await db_session.execute(stmt)
            draft_sessions = result.scalars().unique()

        for draft_session in draft_sessions:
            if not draft_session.session_stage and draft_session.draft_channel_id:
                channel_id = int(draft_session.draft_channel_id)
                channel = bot.get_channel(channel_id)
                if channel:
                    try:
                        message = await channel.fetch_message(int(draft_session.message_id))
                        from views import PersistentView
                        view = PersistentView(bot=bot,
                                            draft_session_id=draft_session.session_id,
                                            session_type=draft_session.session_type,
                                            team_a_name=draft_session.team_a_name,
                                            team_b_name=draft_session.team_b_name,
                                            session_stage=None)
                        await message.edit(view=view)  # Reattach the view
                    except discord.NotFound:
                        # Handle cases where the message or channel might have been deleted
                        print(f"Message or channel not found for session: {draft_session.session_id}")
                    except Exception as e:
                        # Log or handle any other exceptions
                        print(f"Failed to re-register view for session: {draft_session.session_id}, error: {e}")
            elif draft_session.session_stage == "teams":
                channel_id = int(draft_session.draft_channel_id)
                channel = bot.get_channel(channel_id)
                if channel:
                    try:
                        message = await channel.fetch_message(int(draft_session.message_id))
                        from views import PersistentView
                        view = PersistentView(bot=bot,
                                            draft_session_id=draft_session.session_id,
                                            session_type=draft_session.session_type,
                                            team_a_name=draft_session.team_a_name,
                                            team_b_name=draft_session.team_b_name,
                                            session_stage="teams")
                        await message.edit(view=view)  # Reattach the view
                    except discord.NotFound:
                        # Handle cases where the message or channel might have been deleted
                        print(f"Message or channel not found for session: {draft_session.session_id}")
                    except Exception as e:
                        # Log or handle any other exceptions
                        print(f"Failed to re-register view for session: {draft_session.session_id}, error: {e}")
            elif draft_session.session_stage == "pairings":
                # Group match results by pairing_message_id
                matches_by_pairing_msg = {}
                for match_result in draft_session.match_results:
                    pairing_msg_id = match_result.pairing_message_id
                    if pairing_msg_id not in matches_by_pairing_msg:
                        matches_by_pairing_msg[pairing_msg_id] = []
                    matches_by_pairing_msg[pairing_msg_id].append(match_result)
                
                # Iterate over each group of match results by their pairing message
                for pairing_message_id, match_results in matches_by_pairing_msg.items():
                    if draft_session.draft_chat_channel:
                        channel_id = int(draft_session.draft_chat_channel)
                        channel = bot.get_channel(channel_id)
                        if channel:
                            try:
                                message = await channel.fetch_message(int(pairing_message_id))
                                view = View(timeout=None)  # Initialize a new view for this set of match results
                                
                                # Add a button for each match result in this group
                                for match_result in match_results:
                                    from views import MatchResultButton
                                    button = MatchResultButton(
                                        bot=bot,
                                        session_id=draft_session.session_id,
                                        match_id=match_result.id,
                                        match_number=match_result.match_number,
                                        label=f"Match {match_result.match_number} Results",
                                        style=discord.ButtonStyle.primary
                                        # row parameter is optional
                                    )
                                    if draft_session.session_type != "test":
                                        view.add_item(button)
                                
                                # Now, view contains all buttons for the matches associated with this pairing message
                                await message.edit(view=view)
                            except discord.NotFound:
                                print(f"Pairing message or channel not found for pairing message ID: {pairing_message_id}")
                            except Exception as e:
                                print(f"Failed to re-register view for pairing message ID: {pairing_message_id}, error: {e}")

async def calculate_player_standings():
    time = datetime.now()
    async with AsyncSessionLocal() as db_session:
        async with db_session.begin():
            stmt = select(PlayerLimit)
            results = await db_session.execute(stmt)
            
            player_scores = {}

            for row in results.scalars().all():
                player_id = row.player_id
                display_name = row.display_name
                all_points = [row.match_one_points, row.match_two_points, row.match_three_points]
                total_points = sum(all_points)
                top_two_week_points = sum(sorted(all_points, reverse=True)[:2])

                if player_id not in player_scores:
                    player_scores[player_id] = {
                        'display_name': display_name,
                        'total_points': 0,
                        'total_all_points': 0,
                        'drafts_participated': 0
                    }

                player_scores[player_id]['total_points'] += top_two_week_points
                player_scores[player_id]['total_all_points'] += total_points
                player_scores[player_id]['drafts_participated'] += 1

            # Calculate win percentage and sort
            for player_id, details in player_scores.items():
                total_drafts = details['drafts_participated']
                if total_drafts > 0:
                    details['win_percentage'] = (details['total_all_points'] / (3 * total_drafts)) * 100  # Multiply by 100 to get percentage

            sorted_players = sorted(
                player_scores.values(), 
                key=lambda x: (x['total_points'], x.get('win_percentage', 0)),
                reverse=True
            )

            # Prepare the embed
            embed = discord.Embed(
                title="AlphaFrog Prelim Standings",
                description=f"Standings as of <t:{int(time.timestamp())}:F>",
                color=discord.Color.dark_purple()
            )
            standings_text = ""
            for idx, player in enumerate(sorted_players, start=1):
                # Format win percentage to display as an integer percentage
                standings_text += f"\n{idx}. **{player['display_name']}** - {player['total_points']} points (Win %: {player['win_percentage']:.0f}%)"
            
            embed.add_field(name="Standings", value=standings_text, inline=False)
            
            return embed
