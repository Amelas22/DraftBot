import random
from sqlalchemy import update
from session import AsyncSessionLocal, get_draft_session, DraftSession

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


async def calculate_pairings(self, session_id):
        session = await get_draft_session(session_id)
        if not session:
            print("Draft session not found.")
            return
        num_players = len(session.team_a) + len(session.team_b)
        print(f"Number of players {num_players}")
        if num_players not in [6, 8]:
            raise ValueError("Unsupported number of players. Only 6 or 8 players are supported.")

        assert len(self.team_a) == len(self.team_b), "Teams must be of equal size."
        
        session.match_results = {}  
        pairings = {1: [], 2: [], 3: []}

         # Generate pairings
        for round in range(1, 4):
            round_pairings = []
            for i, player_a in enumerate(session.team_a):
                player_b_index = (i + round - 1) % len(session.team_b)
                player_b = session.team_b[player_b_index]

                match_number = session.match_counter
                session.matches[match_number] = {"players": (player_a, player_b), "results": None}
                session.match_results[match_number] = {
                    "player1_id": player_a, "player1_wins": 0, 
                    "player2_id": player_b, "player2_wins": 0,
                    "winner_id": None  
                }
                
                round_pairings.append((player_a, player_b, match_number))
                session.match_counter += 1

            session.pairings[round] = round_pairings

        async with AsyncSessionLocal() as db_session:
                async with db_session.begin():
                    # Update the session in the database
                    await db_session.execute(update(DraftSession)
                                            .where(DraftSession.session_id == session.session_id)
                                            .values(matches=session.matches, match_results=session.match_results,
                                                    pairings=session.pairings, match_counter=session.match_counter))
                    await db_session.commit()
        return pairings