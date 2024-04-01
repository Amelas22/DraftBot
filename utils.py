import random
import discord
from sqlalchemy import update, select
from session import AsyncSessionLocal, get_draft_session, DraftSession, MatchResult
from sqlalchemy.orm import selectinload

import logging


logging.basicConfig(level=logging.INFO)

async def split_into_teams(draft_session_id):
    # Fetch the current draft session to ensure it's up to date.
    draft_session = await get_draft_session(draft_session_id)
    if not draft_session:
        print("The draft session could not be found.")
        return
    
    # Check if there are any sign-ups to split into teams.
    sign_ups = draft_session.sign_ups
    if sign_ups:
        sign_ups_list = list(sign_ups.keys())
        random.shuffle(sign_ups_list)
        mid_point = len(sign_ups_list) // 2
        team_a = sign_ups_list[:mid_point]
        team_b = sign_ups_list[mid_point:]

        async with AsyncSessionLocal() as db_session:
            async with db_session.begin():
                # Update the draft session with the new teams.
                await db_session.execute(update(DraftSession)
                                         .where(DraftSession.session_id == draft_session_id)
                                         .values(team_a=team_a, team_b=team_b))
                await db_session.commit()


async def generate_seating_order(bot, draft_session):
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

    num_players = len(session.team_a) + len(session.team_b)
    if num_players not in [6, 8]:
        raise ValueError("Unsupported number of players. Only 6 or 8 players are supported.")

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


async def post_pairings(bot, guild, session_id):

    async with AsyncSessionLocal() as db_session:
        stmt = select(DraftSession).options(selectinload(DraftSession.match_results)).filter(DraftSession.session_id == session_id)
        session = await db_session.scalar(stmt)
        if not session:
            print("Draft session not found.")
            return
    
    draft_chat_channel_obj = guild.get_channel(int(session.draft_chat_channel))
    if not draft_chat_channel_obj:
        print("Draft chat channel not found.")
        return

    # Group match results by round number
    match_results_by_round = {}
    for match_result in session.match_results:
        round_number = (match_result.match_number - 1) // (len(session.team_a) or 1) + 1
        match_results_by_round.setdefault(round_number, []).append(match_result)

    # Post pairings for each round
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

        await draft_chat_channel_obj.send(embed=embed, view=view)


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
        print(f"Team A wins: {team_a_wins}, Team B Wins: {team_b_wins}")
        return team_a_wins, team_b_wins


async def generate_draft_summary_embed(bot, draft_session_id):
    async with AsyncSessionLocal() as session:
        draft_session = await get_draft_session(draft_session_id)
        if not draft_session:
            print("Draft session not found. Generate Draft Summary")
            return None

        guild = bot.get_guild(int(draft_session.guild_id))
        team_a_names = [guild.get_member(int(user_id)).display_name for user_id in draft_session.team_a]
        team_b_names = [guild.get_member(int(user_id)).display_name for user_id in draft_session.team_b]

        team_a_wins, team_b_wins = await calculate_team_wins(draft_session_id)
        total_matches = draft_session.match_counter - 1
        half_matches = total_matches // 2
        print(f"total_matches: {total_matches}, half_matches: {half_matches}")
        title, description = await determine_draft_outcome(bot, draft_session, team_a_wins, team_b_wins, half_matches, total_matches)

        embed = discord.Embed(title=title, description=description, color=discord.Color.blue())
        team_a_display = f"Team A wins: {team_a_wins}"
        team_b_display = f"Team B wins: {team_b_wins}"
        embed.add_field(name="Team A", value="\n".join(team_a_names), inline=True)
        embed.add_field(name="Team B", value="\n".join(team_b_names), inline=True)
        embed.add_field(name="\u200B", value=team_a_display, inline=False)
        embed.add_field(name="\u200B", value=team_b_display, inline=False)

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
            description = f"Draft Start: <t:{int(draft_session.draft_start_time.timestamp())}:F>"
        elif draft_session.session_type == "premade":
            team_name = draft_session.team_a_name if winner_team_ids == draft_session.team_a else draft_session.team_b_name
            title = f"{team_name} has won the match!"
            description = f"Congratulations to " + ", ".join(member.display_name for member in winner_team if member) + f" on winning the draft!\nDraft Start: <t:{int(draft_session.draft_start_time.timestamp())}:F>"
        else:
            title = "Draft Outcome"
        
        
    # determine if a draw was achieved
    elif team_a_wins == half_matches and team_b_wins == half_matches and total_matches % 2 == 0:
        title = "The Draft is a Draw!"
        description = f"Draft Start: <t:{int(draft_session.draft_start_time.timestamp())}:F>"
    else:
        title = f"Draft-{draft_session.draft_id} Standings"
        description = "If a drafter is missing from this channel, they likely can still see the channel but have the Discord invisible setting on."

    return title, description


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
            logging.error(f"Draft session not found for session_id: {session_id}")
            return None, None  # Draft session not found

    match_result = next((match for match in draft_session.match_results if match.match_number == match_number), None)
    if not match_result:
        logging.error(f"Match result not found within the session for match_number: {match_number}")
        return None, None  # Match not found within the session

    logging.info(f"Fetching details for player1_id: {match_result.player1_id}, player2_id: {match_result.player2_id}")

    guild = bot.get_guild(int(draft_session.guild_id))
    if not guild:
        logging.error("Guild not found.")
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

        # Assuming you have already calculated and updated team wins in the database

        updated_embed = await generate_draft_summary_embed(bot, draft_session_id)
        guild = bot.get_guild(int(draft_session.guild_id))
        channel = guild.get_channel(int(draft_session.draft_chat_channel))
        
        try:
            summary_message = await channel.fetch_message(int(draft_session.draft_summary_message_id))
            await summary_message.edit(embed=updated_embed)
            print("Draft summary message updated successfully.")
        except Exception as e:
            print(f"Failed to update draft summary message: {e}")



async def create_updated_view_for_pairings_message(session_id, match_number):
    view = discord.ui.View(timeout=None)
    # Generate buttons for each match, similar to before, 
    # but ensure they reflect the current state (e.g., disabling the button if the result has been reported)
    # This might involve fetching the current state of all matches in the session

    return view
