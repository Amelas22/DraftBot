import random
import discord
from sqlalchemy import update, select
from session import AsyncSessionLocal, get_draft_session, DraftSession, MatchResult
from sqlalchemy.orm import selectinload

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
                session_id=session.id,
                match_number=session.match_counter,
                player1_id=player_a,
                player1_wins=0,
                player2_id=player_b,
                player2_wins=0,
                winner_id=None
            )
            db_session.add(match_result)
            session.match_counter += 1


async def post_pairings(guild, db_session, session_id):
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

    # Ensure slowmode is off for posting pairings
    await draft_chat_channel_obj.edit(slowmode_delay=0)

    # Group match results by round number
    match_results_by_round = {}
    for match_result in session.match_results:
        round_number = (match_result.match_number - 1) // (len(session.team_a) or 1) + 1
        match_results_by_round.setdefault(round_number, []).append(match_result)

    # Post pairings for each round
    for round_number, match_results in match_results_by_round.items():
        embed = discord.Embed(title=f"Round {round_number} Pairings", color=discord.Color.blue())

        for match_result in match_results:
            player = guild.get_member(int(match_result.player1_id))
            opponent = guild.get_member(int(match_result.player2_id))
            player_name = player.display_name if player else 'Unknown'
            opponent_name = opponent.display_name if opponent else 'Unknown'

            # Formatting the pairings without wins
            match_info = f"**Match {match_result.match_number}**\n{player_name}\n{opponent_name}"
            embed.add_field(name="\u200b", value=match_info, inline=False)

        await draft_chat_channel_obj.send(embed=embed)

